"""
TelegramConnection - Manages a single Telegram account connection.

Handles connection lifecycle, reconnection with exponential backoff,
and event handling for incoming messages.

This is a THIN handler - it captures events and queues them for processing.
All heavy processing (DB queries, user tracking, etc.) happens in the
event processor worker, not here.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from django.db.models import Q
from django.utils import timezone
from telethon import TelegramClient, events
from telethon.errors import AuthKeyDuplicatedError, AuthKeyUnregisteredError, FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import (
    UpdateChannel,
    UpdateChannelParticipant,
    UpdateChatParticipants,
    UpdatePinnedChannelMessages,
)

from accounts.models import GlobalSettings, TelegramAccount
from audit.models import ActivityLog, TelegramChannel
from listener_service.serializers import serialize_for_json
from audit.user_tracking import _extract_user_data
from listeners.handlers import _extract_entities_data, _extract_forward_data, handle_new_message

from .config import get_config
from .db import run_sync_callable
from .event_publisher import EventPublisher

logger = logging.getLogger('trawlr.listener_service.connection')


class TelegramConnection:
    """
    Manages a single Telegram account connection.

    Features:
    - Automatic reconnection with exponential backoff
    - Flood wait handling
    - Thin event handling (capture and queue, no heavy processing)
    - Connection state tracking
    - Multiple event type capture (new message, edited, deleted)
    """

    def __init__(self, account_id: int, event_publisher: Optional[EventPublisher] = None):
        self.account_id = account_id
        self.client: TelegramClient | None = None
        self._config = get_config()
        self._backoff = self._config.initial_backoff
        self._connected = False
        self._stop_event = asyncio.Event()
        self._started_at: datetime | None = None
        self._last_error: str | None = None
        self._reconnect_count = 0
        self._event_publisher = event_publisher

    @property
    def is_connected(self) -> bool:
        """Check if currently connected."""
        return self._connected and self.client is not None

    @property
    def status(self) -> dict:
        """Get current connection status."""
        return {
            'account_id': self.account_id,
            'connected': self._connected,
            'started_at': self._started_at.isoformat() if self._started_at else None,
            'last_error': self._last_error,
            'reconnect_count': self._reconnect_count,
        }

    async def run_forever(self):
        """Main loop - connect and stay connected until stopped."""
        self._started_at = datetime.utcnow()
        logger.info(f"Starting connection for account {self.account_id}")

        while not self._stop_event.is_set():
            try:
                await self._connect()
                await self._run_until_disconnect()
            except FloodWaitError as e:
                logger.warning(f"Account {self.account_id} flood wait: {e.seconds}s")
                await self._update_status('flood_wait', f'Flood wait: {e.seconds}s')
                await self._sleep_or_stop(e.seconds)
            except AuthKeyUnregisteredError:
                logger.error(f"Account {self.account_id} auth key unregistered")
                await self._update_status('error', 'Session expired - re-authentication required', is_authenticated=False)
                # Don't retry - session is invalid
                break
            except AuthKeyDuplicatedError:
                logger.error(f"Account {self.account_id} auth key used from multiple IPs - session invalidated")
                await self._update_status('error', 'Session invalidated - used from multiple IP addresses simultaneously. Re-authentication required.', is_authenticated=False)
                # Don't retry - session is permanently invalid
                break
            except asyncio.CancelledError:
                logger.info(f"Account {self.account_id} connection cancelled")
                break
            except Exception as e:
                self._last_error = str(e)
                self._reconnect_count += 1
                logger.exception(f"Account {self.account_id} error: {e}")
                await self._update_status('error', str(e))
                await self._sleep_or_stop(self._backoff)
                self._backoff = min(self._backoff * 2, self._config.max_backoff)
            else:
                # Clean disconnect - reset backoff
                self._backoff = self._config.initial_backoff

        await self._cleanup()

    async def _sleep_or_stop(self, seconds: int):
        """Sleep for specified seconds, but wake up early if stop requested."""
        try:
            await asyncio.wait_for(
                self._stop_event.wait(),
                timeout=seconds
            )
        except asyncio.TimeoutError:
            pass  # Normal timeout, continue

    async def _connect(self):
        """Establish connection to Telegram."""
        account = await run_sync_callable(
            lambda: TelegramAccount.objects.get(pk=self.account_id)
        )

        logger.info(f"Connecting account {self.account_id} ({account.phone_number})")

        # Use StringSession from database (same as TelegramService)
        session_string = ""
        if account.session_string:
            session_string = account.session_string.strip()
            logger.info(f"Using existing session (length: {len(session_string)})")
        else:
            logger.warning(f"Account {self.account_id} has no session_string in database")

        self.client = TelegramClient(
            StringSession(session_string),
            int(account.api_id),
            account.api_hash,
            device_model="Trawlr Listener",
            system_version="1.0",
            app_version="1.0",
            receive_updates=True,
        )

        await self.client.connect()

        if not await self.client.is_user_authorized():
            raise Exception("Account not authorized - session may have expired")

        # Register event handlers - capture all relevant events
        self.client.add_event_handler(
            self._on_new_message,
            events.NewMessage()
        )
        self.client.add_event_handler(
            self._on_message_edited,
            events.MessageEdited()
        )
        self.client.add_event_handler(
            self._on_message_deleted,
            events.MessageDeleted()
        )
        self.client.add_event_handler(
            self._on_chat_action,
            events.ChatAction()
        )

        # Raw update handlers for channel-level events
        self.client.add_event_handler(
            self._on_channel_update,
            events.Raw(types=[UpdateChannel])
        )
        self.client.add_event_handler(
            self._on_channel_participants,
            events.Raw(types=[UpdateChatParticipants])
        )
        self.client.add_event_handler(
            self._on_channel_participant,
            events.Raw(types=[UpdateChannelParticipant])
        )
        self.client.add_event_handler(
            self._on_channel_pinned,
            events.Raw(types=[UpdatePinnedChannelMessages])
        )

        self._connected = True
        self._last_error = None
        await self._update_status('running')
        logger.info(f"Account {self.account_id} connected successfully")

    async def _run_until_disconnect(self):
        """Run the client until disconnected or stopped."""
        while not self._stop_event.is_set():
            # Check if account still exists and should be running
            try:
                account = await run_sync_callable(
                    lambda: TelegramAccount.objects.get(pk=self.account_id)
                )

                if not account.is_active:
                    logger.info(f"Account {self.account_id} deactivated, stopping")
                    break

            except Exception as e:
                logger.warning(f"Error checking account {self.account_id}: {e}")

            # Check client connection
            if self.client and not self.client.is_connected():
                logger.warning(f"Account {self.account_id} disconnected, will reconnect")
                break

            await self._sleep_or_stop(5)

    async def _auto_discover_channel(self, chat_id, chat_entity):
        """
        Auto-discover a new channel/group from a Telethon entity.
        Creates TelegramChannel + ChannelConfig if auto_discover_sources is enabled
        and the channel doesn't exist yet. Returns the ChannelConfig or None.
        """
        from telethon.tl.types import Channel, Chat, User

        def do_discover():
            settings = GlobalSettings.get_settings()
            if not settings.auto_discover_sources:
                return None

            # Determine channel type from entity
            if isinstance(chat_entity, Channel):
                channel_type = 'supergroup' if chat_entity.megagroup else 'channel'
                is_private = not chat_entity.username
            elif isinstance(chat_entity, Chat):
                channel_type = 'group'
                is_private = True
            elif isinstance(chat_entity, User):
                channel_type = 'private'
                is_private = True
            else:
                return None

            # Use the entity's canonical ID
            entity_id = chat_entity.id

            # Check if already exists (race condition safe with update_or_create)
            account = TelegramAccount.objects.get(pk=self.account_id)
            channel, created = TelegramChannel.objects.update_or_create(
                telegram_id=entity_id,
                defaults={
                    'account': account,
                    'title': getattr(chat_entity, 'title', None) or getattr(chat_entity, 'first_name', 'Unknown') or 'Unknown',
                    'username': getattr(chat_entity, 'username', None),
                    'channel_type': channel_type,
                    'is_private': is_private,
                    'member_count': getattr(chat_entity, 'participants_count', 0) or 0,
                    'joined_at': getattr(chat_entity, 'date', None),
                    'is_forum': getattr(chat_entity, 'forum', False) or False,
                }
            )

            if not created:
                # Already existed — return its config
                try:
                    return channel.config
                except TelegramChannel.config.RelatedObjectDoesNotExist:
                    return None

            # Newly created — log and optionally onboard
            logger.info(f"Auto-discovered source: {channel.title} (type={channel_type}, id={entity_id})")
            ActivityLog.log(
                'source_auto_discovered',
                f'Auto-discovered {channel_type}: {channel.title}',
                source='listener',
                channel=channel,
            )

            if settings.run_onboarding_for_new_sources:
                from tasks import dispatch_task
                from tasks.onboarding import run_channel_onboarding
                channel.onboarded = True
                channel.save(update_fields=['onboarded'])
                try:
                    dispatch_task(
                        run_channel_onboarding,
                        'onboarding',
                        channel=channel,
                        args=(channel.pk,),
                    )
                    logger.info(f"Queued onboarding for auto-discovered source: {channel.title}")
                except Exception as e:
                    logger.error(f"Failed to queue onboarding for {channel.title}: {e}")

            try:
                return channel.config
            except TelegramChannel.config.RelatedObjectDoesNotExist:
                return None

        try:
            return await run_sync_callable(do_discover)
        except Exception as e:
            logger.error(f"Error auto-discovering channel {chat_id}: {e}")
            return None

    async def _on_new_message(self, event):
        """
        Handle incoming messages.

        If event_publisher is configured, uses thin handler pattern (queue event).
        Otherwise falls back to direct handler for backwards compatibility.
        """
        try:
            chat_id = event.chat_id
            message_id = event.message.id
            logger.info(f"Account {self.account_id} received message {message_id} in chat {chat_id}")

            if self._event_publisher:
                # Thin handler - capture and queue
                await self._queue_new_message_event(event)
            else:
                # Legacy fallback - direct handler
                account = await run_sync_callable(
                    lambda: TelegramAccount.objects.get(pk=self.account_id)
                )
                await handle_new_message(event, account)
        except Exception as e:
            logger.exception(f"Error handling message for account {self.account_id}: {e}")

    async def _queue_new_message_event(self, event):
        """
        Extract data from event and queue for processing.
        This is the thin handler - minimal processing, maximum reliability.
        """
        message = event.message
        chat_id = event.chat_id

        # Extract sender data (must be done in async context before queuing)
        sender = None
        sender_data = None
        if message.sender_id:
            try:
                sender = await message.get_sender()
                sender_data = _extract_user_data(sender)
            except Exception as e:
                logger.debug(f"Could not get sender: {e}")

        # Extract media info
        media_info = self._extract_media_info(message)

        # Extract entities data
        entities_data = []
        if message.entities:
            entities_data = _extract_entities_data(message.message or '', message.entities)

        # Extract forward data
        forward_data = None
        if message.forward:
            forward_data = _extract_forward_data(message.forward)

        # Check channel config for thumbnail settings (download happens in worker)
        try:
            # Build lookup IDs for all possible chat ID formats
            abs_id = abs(chat_id)
            lookup_ids = [
                chat_id,                    # Original ID as-is
                abs_id,                     # Positive version
                -abs_id,                    # Negative version
                int(f"-100{abs_id}"),       # Full channel format: -100 + abs
            ]
            # If chat_id already has -100 prefix, also try without it
            chat_str = str(chat_id)
            if chat_str.startswith('-100') and len(chat_str) > 4:
                lookup_ids.append(int(chat_str[4:]))
                lookup_ids.append(-int(chat_str[4:]))
            # Remove duplicates
            lookup_ids = list(dict.fromkeys(lookup_ids))

            def get_channel_config():
                query = Q()
                for lid in lookup_ids:
                    query |= Q(telegram_id=lid)
                channel = TelegramChannel.objects.select_related('config').filter(query).first()
                if channel:
                    try:
                        return channel.config
                    except TelegramChannel.config.RelatedObjectDoesNotExist:
                        return None
                return None

            config = await run_sync_callable(get_channel_config)

            # Auto-discover: if channel not found, try to create it
            if config is None and event.chat:
                config = await self._auto_discover_channel(chat_id, event.chat)

            # Pass thumbnail config to worker instead of downloading here
            if config and config.download_thumbnails and media_info['has_media']:
                media_info['download_thumbnail'] = True
                media_info['thumbnail_size'] = config.thumbnail_size
        except Exception as e:
            logger.debug(f"Could not get channel config: {e}")

        # Extract reply/topic info from MessageReplyHeader
        reply_to_message_id = None
        reply_to_top_id = None
        is_topic_message = False
        if message.reply_to:
            reply_to_message_id = getattr(message.reply_to, 'reply_to_msg_id', None)
            reply_to_top_id = getattr(message.reply_to, 'reply_to_top_id', None)
            # forum_topic=True means this is a post in a topic, not a direct reply
            is_topic_message = getattr(message.reply_to, 'forum_topic', False) or False

        # Build message data
        message_data = {
            'text': message.text or '',
            'sender_id': message.sender_id,
            'date': message.date.isoformat() if message.date else None,
            'edit_date': message.edit_date.isoformat() if message.edit_date else None,
            'reply_to_message_id': reply_to_message_id,
            'reply_to_top_id': reply_to_top_id,  # Topic ID when in forum
            'is_topic_message': is_topic_message,  # True = post in topic, False = direct reply
            'views': message.views or 0,
            'forwards': message.forwards or 0,
            'is_pinned': getattr(message, 'pinned', False) or False,
            'is_post': getattr(message, 'post', False) or False,
            'noforwards': getattr(message, 'noforwards', False) or False,
            'is_silent': getattr(message, 'silent', False) or False,
            'grouped_id': getattr(message, 'grouped_id', None),
            'post_author': getattr(message, 'post_author', '') or '',
            'ttl_period': getattr(message, 'ttl_period', None),
            # Peer type for deduplication: 'channel' (supergroup/channel), 'chat' (legacy group), 'user' (DM)
            'peer_type': message.peer_id.__class__.__name__.replace('Peer', '').lower() if message.peer_id else 'unknown',
            'reactions': self._serialize_reactions(message.reactions),
        }

        # Serialize the full raw Telethon message for archival
        tgraw = serialize_for_json(message)

        # Publish to queue (thumbnail downloaded by worker, not here)
        logger.debug(f"Publishing msg={message.id} chat={chat_id} peer_type={message_data['peer_type']}")

        await self._event_publisher.publish_new_message(
            account_id=self.account_id,
            chat_id=chat_id,
            message_id=message.id,
            message_data=message_data,
            sender_data=sender_data,
            media_info=media_info,
            entities_data=entities_data,
            forward_data=forward_data,
            tgraw=tgraw,
        )

    @staticmethod
    def _serialize_reactions(reactions) -> dict:
        """
        Serialize Telethon MessageReactions to a simple dict.
        Returns e.g. {"👍": 42, "❤️": 15, "custom:12345": 3}
        """
        if not reactions or not hasattr(reactions, 'results') or not reactions.results:
            return {}

        result = {}
        for reaction_count in reactions.results:
            reaction = reaction_count.reaction
            count = reaction_count.count or 0

            # ReactionEmoji has .emoticon, ReactionCustomEmoji has .document_id
            if hasattr(reaction, 'emoticon'):
                key = reaction.emoticon
            elif hasattr(reaction, 'document_id'):
                key = f"custom:{reaction.document_id}"
            else:
                continue

            result[key] = count

        return result

    def _extract_media_info(self, message) -> dict:
        """Extract media information from a message.

        Note on file IDs:
        - file_id: The Telegram object ID (photo.id, document.id) - stable across all accounts
        - file_unique_id: Same as file_id in MTProto - this IS the globally unique identifier
          (In Bot API, file_unique_id is derived from this same ID)
        """
        has_media = bool(message.media)
        media_type = ''
        file_id = ''
        file_unique_id = ''
        filename = ''
        file_size = 0
        mime_type = ''
        media_width = None
        media_height = None
        media_duration = None

        if message.photo:
            media_type = 'photo'
            filename = f"photo_{message.id}.jpg"
            # photo.id is the stable unique identifier across all accounts
            file_id = str(message.photo.id)
            file_unique_id = str(message.photo.id)
            mime_type = 'image/jpeg'
            # Get dimensions from the largest photo size
            if message.photo.sizes:
                largest = message.photo.sizes[-1]
                media_width = getattr(largest, 'w', None)
                media_height = getattr(largest, 'h', None)
                file_size = getattr(largest, 'size', 0) or 0
        elif message.video:
            media_type = 'video'
            filename = getattr(message.video, 'file_name', '') or f"video_{message.id}.mp4"
            file_size = message.video.size or 0
            mime_type = message.video.mime_type or ''
            # video is a Document type, document.id is globally unique
            file_id = str(message.video.id)
            file_unique_id = str(message.video.id)
            # Extract dimensions and duration from video attributes
            for attr in message.video.attributes:
                if hasattr(attr, 'w'):
                    media_width = attr.w
                    media_height = attr.h
                if hasattr(attr, 'duration'):
                    media_duration = attr.duration
        elif message.document:
            media_type = 'file'
            filename = getattr(message.document, 'file_name', '') or f"file_{message.id}"
            file_size = message.document.size or 0
            mime_type = message.document.mime_type or ''
            # document.id is globally unique across all of Telegram
            file_id = str(message.document.id)
            file_unique_id = str(message.document.id)
            # Some documents (e.g. GIFs, audio) may have dimensions/duration
            for attr in (message.document.attributes or []):
                if hasattr(attr, 'w'):
                    media_width = attr.w
                    media_height = attr.h
                if hasattr(attr, 'duration'):
                    media_duration = attr.duration

        return {
            'has_media': has_media,
            'media_type': media_type,
            'file_id': file_id,
            'file_unique_id': file_unique_id,
            'filename': filename,
            'file_size': file_size,
            'mime_type': mime_type,
            'media_width': media_width,
            'media_height': media_height,
            'media_duration': media_duration,
        }

    async def _on_message_edited(self, event):
        """Handle message edited events."""
        try:
            if not self._event_publisher:
                return  # No-op without event publisher

            message = event.message
            chat_id = event.chat_id
            logger.debug(f"Account {self.account_id} message {message.id} edited in chat {chat_id}")

            message_data = {
                'text': message.text or '',
                'edit_date': message.edit_date.isoformat() if message.edit_date else None,
                'reactions': self._serialize_reactions(message.reactions),
            }

            tgraw = serialize_for_json(message)

            await self._event_publisher.publish_message_edited(
                account_id=self.account_id,
                chat_id=chat_id,
                message_id=message.id,
                message_data=message_data,
                tgraw=tgraw,
            )
        except Exception as e:
            logger.exception(f"Error handling message edit for account {self.account_id}: {e}")

    async def _on_message_deleted(self, event):
        """Handle message deleted events."""
        try:
            if not self._event_publisher:
                return  # No-op without event publisher

            chat_id = event.chat_id
            message_ids = event.deleted_ids
            logger.debug(f"Account {self.account_id} messages {message_ids} deleted in chat {chat_id}")

            await self._event_publisher.publish_message_deleted(
                account_id=self.account_id,
                chat_id=chat_id,
                message_ids=message_ids,
            )
        except Exception as e:
            logger.exception(f"Error handling message delete for account {self.account_id}: {e}")

    async def _on_chat_action(self, event):
        """Handle chat action events (user joins/leaves, etc.)."""
        try:
            if not self._event_publisher:
                return  # No-op without event publisher

            chat_id = event.chat_id
            user_id = event.user_id

            # Determine action type
            action_type = 'unknown'
            if event.user_joined:
                action_type = 'user_joined'
            elif event.user_left:
                action_type = 'user_left'
            elif event.user_kicked:
                action_type = 'user_kicked'
            elif event.user_added:
                action_type = 'user_added'
            elif event.new_title:
                action_type = 'title_changed'
            elif event.new_photo:
                action_type = 'photo_changed'

            logger.debug(f"Account {self.account_id} chat action: {action_type} in chat {chat_id}")

            # Auto-discover channel if not yet known
            if event.chat:
                await self._auto_discover_channel(chat_id, event.chat)

            # Extract user data for user-related actions
            user_data = None
            if user_id and action_type in ('user_joined', 'user_left', 'user_kicked', 'user_added'):
                try:
                    user = await event.get_user()
                    user_data = _extract_user_data(user)
                except Exception as e:
                    logger.debug(f"Could not get user data for chat action: {e}")

            action_data = {
                'new_title': event.new_title if hasattr(event, 'new_title') else None,
            }

            await self._event_publisher.publish_chat_action(
                account_id=self.account_id,
                chat_id=chat_id,
                action_type=action_type,
                user_id=user_id,
                action_data=action_data,
                user_data=user_data,
            )
        except Exception as e:
            logger.exception(f"Error handling chat action for account {self.account_id}: {e}")

    async def _on_channel_update(self, update: UpdateChannel):
        """Handle raw UpdateChannel events (channel metadata changes)."""
        try:
            if not self._event_publisher:
                return

            channel_id = update.channel_id
            logger.debug(f"Account {self.account_id} received UpdateChannel for channel {channel_id}")

            # Serialize the raw update to dict for storage
            raw_update = {
                'channel_id': channel_id,
                '_type': 'UpdateChannel',
            }

            await self._event_publisher.publish_channel_update(
                account_id=self.account_id,
                channel_id=channel_id,
                raw_update=raw_update,
            )
        except Exception as e:
            logger.exception(f"Error handling UpdateChannel for account {self.account_id}: {e}")

    async def _on_channel_participants(self, update: UpdateChatParticipants):
        """Handle raw UpdateChatParticipants events (participant list changes)."""
        try:
            if not self._event_publisher:
                return

            # UpdateChatParticipants has a 'participants' field with ChatParticipants object
            participants = update.participants
            chat_id = participants.chat_id if hasattr(participants, 'chat_id') else None
            logger.debug(f"Account {self.account_id} received UpdateChatParticipants for chat {chat_id}")

            # Serialize the raw update to dict for storage
            raw_update = {
                'chat_id': chat_id,
                'participants_type': type(participants).__name__,
                '_type': 'UpdateChatParticipants',
            }

            await self._event_publisher.publish_channel_participants(
                account_id=self.account_id,
                channel_id=chat_id or 0,
                raw_update=raw_update,
            )
        except Exception as e:
            logger.exception(f"Error handling UpdateChatParticipants for account {self.account_id}: {e}")

    async def _on_channel_participant(self, update: UpdateChannelParticipant):
        """Handle raw UpdateChannelParticipant events (individual member changes in supergroups)."""
        try:
            if not self._event_publisher:
                return

            channel_id = update.channel_id
            user_id = update.user_id
            actor_id = update.actor_id  # Who made the change
            date = update.date

            # Get participant status info
            prev_participant = update.prev_participant
            new_participant = update.new_participant

            logger.debug(
                f"Account {self.account_id} received UpdateChannelParticipant: "
                f"channel={channel_id}, user={user_id}, actor={actor_id}"
            )

            # Serialize the raw update to dict for storage
            raw_update = {
                'channel_id': channel_id,
                'user_id': user_id,
                'actor_id': actor_id,
                'date': date.isoformat() if date else None,
                'prev_participant_type': type(prev_participant).__name__ if prev_participant else None,
                'new_participant_type': type(new_participant).__name__ if new_participant else None,
                '_type': 'UpdateChannelParticipant',
            }

            await self._event_publisher.publish_channel_participant(
                account_id=self.account_id,
                channel_id=channel_id,
                user_id=user_id,
                raw_update=raw_update,
            )
        except Exception as e:
            logger.exception(f"Error handling UpdateChannelParticipant for account {self.account_id}: {e}")

    async def _on_channel_pinned(self, update: UpdatePinnedChannelMessages):
        """Handle raw UpdatePinnedChannelMessages events (pinned message changes)."""
        try:
            if not self._event_publisher:
                return

            channel_id = update.channel_id
            message_ids = update.messages  # List of pinned message IDs
            pinned = update.pinned  # True if pinned, False if unpinned
            logger.debug(f"Account {self.account_id} received UpdatePinnedChannelMessages: channel={channel_id}, messages={message_ids}, pinned={pinned}")

            # Serialize the raw update to dict for storage
            raw_update = {
                'channel_id': channel_id,
                'message_ids': list(message_ids),
                'pinned': pinned,
                '_type': 'UpdatePinnedChannelMessages',
            }

            # Use first message_id if available
            message_id = message_ids[0] if message_ids else None

            await self._event_publisher.publish_channel_pinned(
                account_id=self.account_id,
                channel_id=channel_id,
                message_id=message_id,
                raw_update=raw_update,
            )
        except Exception as e:
            logger.exception(f"Error handling UpdatePinnedChannelMessages for account {self.account_id}: {e}")

    async def _update_status(self, status: str, error: str = '', is_authenticated: bool | None = None):
        """Update account listener status in database.

        Args:
            status: The listener status ('running', 'stopped', 'error', 'flood_wait')
            error: Optional error message
            is_authenticated: If not None, update the is_authenticated field
        """
        def update_account():
            account = TelegramAccount.objects.get(pk=self.account_id)
            account.listener_status = status
            account.listener_error = error
            if status == 'running':
                account.listener_started_at = timezone.now()
            if is_authenticated is not None:
                account.is_authenticated = is_authenticated
            account.save()

        try:
            await run_sync_callable(update_account)
        except Exception as e:
            logger.warning(f"Failed to update status for account {self.account_id}: {e}")

    async def _cleanup(self):
        """Clean up resources."""
        self._connected = False

        if self.client:
            try:
                await self.client.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting account {self.account_id}: {e}")
            self.client = None

        await self._update_status('stopped')
        logger.info(f"Account {self.account_id} connection cleaned up")

    async def stop(self):
        """Signal the connection to stop."""
        logger.info(f"Stop requested for account {self.account_id}")
        self._stop_event.set()

    async def disconnect(self):
        """Stop and wait for cleanup."""
        await self.stop()
        # Give some time for cleanup
        await asyncio.sleep(0.5)
