"""
Telegram event handlers for new messages.
"""

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from asgiref.sync import sync_to_async
from django.db.models import F, Q
from telethon.errors import FloodWaitError

from accounts.models import GlobalSettings
from audit.models import ActivityLog, ChannelConfig, ExclusionRule, ForwardSource, GlobalEntity, MessageEntity, TelegramChannel
from storage.utils import get_storage_backend, is_cloud_backend
from audit.user_tracking import download_user_profile_photo_async, track_user_from_message_async
from downloads.consumers import sync_broadcast_queue_update
from downloads.models import ArchivedMessage, DownloadedFile, DownloadTask
from listener_service.serializers import serialize_for_json as _serialize_for_json

logger = logging.getLogger('trawlr.listeners')

# Temporary flag to enable event capture for debugging
# This will only capture events from a specific source
CAPTURE_EVENTS = True
CAPTURE_CHANNEL_PK = 1  # Only capture from this source - use the primary ID (django pk)
CAPTURE_DIR = Path(__file__).parent.parent / 'docs' / 'event_captures'


async def _capture_event(event, label, channel_pk):
    """Save raw event data to JSON file for analysis."""
    if not CAPTURE_EVENTS or channel_pk != CAPTURE_CHANNEL_PK:
        return

    try:
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

        message = event.message

        # Determine event type for filename
        event_type = 'text'
        if message.photo:
            event_type = 'photo'
        elif message.video:
            event_type = 'video'
        elif message.document:
            event_type = 'document'
        elif message.sticker:
            event_type = 'sticker'
        elif message.audio:
            event_type = 'audio'
        elif message.voice:
            event_type = 'voice'
        elif message.video_note:
            event_type = 'video_note'
        elif message.gif:
            event_type = 'gif'

        if message.forward:
            event_type = f'forward_{event_type}'
        if message.reply_to:
            event_type = f'reply_{event_type}'

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'{timestamp}_{event_type}_{message.id}.json'
        filepath = CAPTURE_DIR / filename

        # Capture comprehensive event data
        data = {
            'capture_label': label,
            'captured_at': datetime.now().isoformat(),
            'event': _serialize_for_json(event),
            'message': _serialize_for_json(message),
            'chat': _serialize_for_json(event.chat),
            'sender': _serialize_for_json(message.sender),
            'media': _serialize_for_json(message.media),
            'forward': _serialize_for_json(message.forward),
            'reply_to': _serialize_for_json(message.reply_to),
        }

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, default=str)

        logger.info(f"Captured event to {filepath}")
    except Exception as e:
        logger.exception(f"Failed to capture event: {e}")


async def _log_activity(activity_type, description, source='listener', channel=None, **details):
    """
    Log an activity to the ActivityLog table.

    This is an async wrapper around the sync ActivityLog.log() method.
    Failures are logged but don't interrupt message processing.
    """
    try:
        @sync_to_async
        def create_log():
            return ActivityLog.log(
                activity_type=activity_type,
                description=description,
                source=source,
                channel=channel,
                **details
            )

        await create_log()
    except Exception as e:
        logger.debug(f"Failed to log activity: {e}")


async def _check_user_exclusion(telegram_user_id, channel_id):
    """
    Check if a user is excluded from message processing (global or source-specific).
    Returns the ExclusionRule if excluded, None otherwise.

    Uses a single optimized query that checks both global and source-specific exclusions.
    """
    @sync_to_async
    def _check():
        # Single query: check global OR source-specific exclusion
        # Order by is_global DESC so global exclusions are returned first if both exist
        return ExclusionRule.objects.filter(
            telegram_user__telegram_id=telegram_user_id,
            is_active=True
        ).filter(
            Q(is_global=True) | Q(source_id=channel_id, is_global=False)
        ).order_by('-is_global').first()

    return await _check()


async def _increment_exclusion_trigger(exclusion_id):
    """Increment the trigger count for an exclusion rule."""
    @sync_to_async
    def _increment():
        ExclusionRule.objects.filter(pk=exclusion_id).update(
            trigger_count=F('trigger_count') + 1
        )

    await _increment()


async def handle_new_message(event, account):
    """
    Handle a new message event from Telegram.

    This is called by the Telethon client when a new message
    is received in any channel/group the account is a member of.
    """
    try:
        # Get the chat/channel ID
        chat_id = event.chat_id
        logger.info(f"Handler processing message from chat {chat_id}")

        # Normalize chat_id - Telethon returns negative IDs for channels/groups
        # but we may have stored positive IDs
        lookup_ids = [chat_id]
        if chat_id < 0:
            # Try positive version (strip -100 prefix for channels)
            if str(chat_id).startswith('-100'):
                lookup_ids.append(int(str(chat_id)[4:]))
            else:
                lookup_ids.append(abs(chat_id))
        else:
            # Try negative versions
            lookup_ids.append(-chat_id)
            lookup_ids.append(int(f"-100{chat_id}"))

        # Find the channel in our database
        @sync_to_async
        def get_channel():
            try:
                query = Q()
                for lid in lookup_ids:
                    query |= Q(telegram_id=lid)
                return TelegramChannel.objects.select_related('config').filter(
                    query,
                    account=account
                ).first()
            except TelegramChannel.DoesNotExist:
                return None

        channel = await get_channel()
        if channel is None:
            # Channel not tracked, ignore
            logger.debug(f"Chat {chat_id} not tracked (lookup_ids: {lookup_ids})")
            return

        logger.info(f"Found tracked channel: {channel.title} (pk={channel.pk})")

        # Capture raw event data for debugging
        await _capture_event(event, f"channel_{channel.pk}", channel.pk)

        # Get or create config - this should exist (auto-created by signal) but handle legacy channels
        try:
            config = channel.config
        except TelegramChannel.config.RelatedObjectDoesNotExist:
            logger.warning(f"Channel {channel.title} ({channel.pk}) missing config - creating now")
            config, _ = await sync_to_async(ChannelConfig.objects.get_or_create)(channel=channel)

        # Check bypass_listener setting - skip processing if enabled
        if config.bypass_listener:
            logger.debug(f"Bypassing listener for {channel.title} (bypass_listener=True)")
            return

        message = event.message
        sender_id = message.sender_id

        # Get the sender - use async get_sender() which properly resolves entities
        # message.sender is just cached, but get_sender() will fetch if needed
        sender = None
        if sender_id:
            try:
                # get_sender() is the proper async method that handles entity resolution
                sender = await message.get_sender()
                if sender:
                    logger.debug(f"Got sender entity for user {sender_id}: {getattr(sender, 'first_name', 'Unknown')}")
            except FloodWaitError as e:
                # Don't block message processing for rate limits
                logger.warning(f"FloodWaitError fetching sender {sender_id}: {e.seconds}s wait required, skipping user details")
                sender = None
            except Exception as e:
                logger.debug(f"Could not get sender for {sender_id}: {e}")
                sender = None

        # Track user if sender is available
        if sender:
            user, created = await track_user_from_message_async(sender, channel, message.date)
            if created:
                await _log_activity(
                    'user_tracked',
                    f"Tracked user: {getattr(sender, 'first_name', '')} (@{getattr(sender, 'username', '')})",
                    channel=channel,
                    telegram_user=user,
                )
            # Download profile photo if user was tracked and setting is enabled (pass sender to avoid extra API call)
            if user and account.download_profile_photos:
                try:
                    photo_downloaded = await download_user_profile_photo_async(event.client, sender_id, user, sender=sender)
                    if photo_downloaded:
                        await _log_activity(
                            'photo_downloaded',
                            f"Downloaded profile photo for {getattr(sender, 'first_name', '')}",
                            channel=channel,
                            user_id=sender_id,
                        )
                except FloodWaitError as e:
                    # Don't block message processing for photo download rate limits
                    logger.warning(f"FloodWaitError downloading profile photo for {sender_id}: {e.seconds}s wait required")
                    await _log_activity(
                        'flood_wait',
                        f"Flood wait {e.seconds}s for profile photo download",
                        channel=channel,
                        wait_seconds=e.seconds,
                    )

        # Check exclusion list - drop message if user is excluded (but user tracking above still happened)
        if sender_id:
            exclusion = await _check_user_exclusion(sender_id, channel.pk)
            if exclusion:
                await _increment_exclusion_trigger(exclusion.pk)
                exclusion_type = "global" if exclusion.is_global else f"source:{channel.title}"
                logger.debug(f"Exclusion triggered ({exclusion_type}): user {sender_id} in channel {channel.pk}")
                return  # Drop message, do not archive or queue download

        # Extract sender info for archived message
        sender_name = ''
        sender_username = ''
        if sender:
            sender_name = getattr(sender, 'first_name', '') or ''
            if hasattr(sender, 'last_name') and sender.last_name:
                sender_name += f" {sender.last_name}"
            sender_username = getattr(sender, 'username', '') or ''

        # Determine media info
        has_media = bool(message.media)
        media_type = ''
        file_id = ''
        filename = ''
        file_size = 0
        mime_type = ''
        thumbnail_path = ''
        media_width = None
        media_height = None
        media_duration = None

        if message.photo:
            media_type = 'photo'
            filename = f"photo_{message.id}.jpg"
            file_id = str(message.photo.id)
            mime_type = 'image/jpeg'
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
            file_id = str(message.video.id)
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
            file_id = str(message.document.id)
            for attr in (message.document.attributes or []):
                if hasattr(attr, 'w'):
                    media_width = attr.w
                    media_height = attr.h
                if hasattr(attr, 'duration'):
                    media_duration = attr.duration

        # Download thumbnail if enabled and message has media
        if has_media and config.download_thumbnails:
            thumbnail_path = await _download_thumbnail(
                event.client,
                message,
                channel.telegram_id,
                message.id,
                config.thumbnail_size
            )

        # Extract message flags (safely with defaults)
        is_pinned = getattr(message, 'pinned', False) or False
        is_post = getattr(message, 'post', False) or False
        noforwards = getattr(message, 'noforwards', False) or False
        is_silent = getattr(message, 'silent', False) or False
        grouped_id = getattr(message, 'grouped_id', None)
        post_author = getattr(message, 'post_author', '') or ''
        ttl_period = getattr(message, 'ttl_period', None)

        # Archive the message (always archive all messages now)
        archived_msg = await _async_create_archived_message(
            channel=channel,
            message_id=message.id,
            text=message.text or '',
            has_media=has_media,
            media_type=media_type,
            telegram_file_id=file_id,
            original_filename=filename,
            file_size=file_size,
            mime_type=mime_type,
            thumbnail_path=thumbnail_path,
            media_width=media_width,
            media_height=media_height,
            media_duration=media_duration,
            sender_id=sender_id,
            sender_name=sender_name,
            sender_username=sender_username,
            reply_to_message_id=message.reply_to_msg_id if message.reply_to else None,
            views=message.views or 0,
            forwards=message.forwards or 0,
            telegram_date=message.date,
            edited_date=message.edit_date,
            is_pinned=is_pinned,
            is_post=is_post,
            noforwards=noforwards,
            is_silent=is_silent,
            grouped_id=grouped_id,
            post_author=post_author,
            ttl_period=ttl_period,
        )

        # Log message processed
        if archived_msg:
            await _log_activity(
                'message_processed',
                f"Processed message {message.id}" + (f" with {media_type}" if media_type else ""),
                channel=channel,
                message_id=message.id,
                has_media=has_media,
                media_type=media_type,
            )

        # Extract and store message entities (URLs, mentions, hashtags, etc.)
        # Extract entity data upfront to avoid accessing Telethon objects inside sync_to_async
        # Use message.message (plain text) not message.text (may include formatting)
        # Entity offsets are relative to the plain text
        if archived_msg and message.entities:
            entities_data = _extract_entities_data(message.message or '', message.entities)
            if entities_data:
                await _async_create_message_entities(archived_msg, entities_data)

        # Extract and store forward source info
        # Extract forward data upfront to avoid accessing Telethon objects inside sync_to_async
        if archived_msg and message.forward:
            forward_data = _extract_forward_data(message.forward)
            if forward_data:
                await _async_create_forward_source(archived_msg, forward_data)

        # Queue for download if auto-download enabled
        if config.auto_download_enabled and not config.is_paused and has_media:
            should_download = False
            if media_type == 'photo' and config.download_photos:
                should_download = True
            elif media_type == 'video' and config.download_videos:
                should_download = True
            elif media_type == 'file' and config.download_files:
                should_download = True

            if should_download:
                effective_priority = config.priority + config.get_file_type_priority(media_type)
                task_created = await _async_create_download_task(
                    channel=channel,
                    message_id=message.id,
                    telegram_file_id=file_id,
                    original_filename=filename,
                    file_type=media_type,
                    file_size=file_size,
                    mime_type=mime_type,
                    priority=effective_priority,
                )
                if task_created:
                    logger.info(f"Queued download: {filename} from {channel.title}")
                    await _log_activity(
                        'download_queued',
                        f"Queued {media_type}: {filename}",
                        channel=channel,
                        message_id=message.id,
                        filename=filename,
                        file_type=media_type,
                        file_size=file_size,
                    )

    except Exception as e:
        logger.exception(f"Error handling new message: {e}")


async def _download_thumbnail(client, message, channel_id, message_id, thumbnail_size='m'):
    """
    Download thumbnail for a message's media.
    Returns the relative path to the thumbnail, or empty string if failed.

    For photos: Downloads the configured size (s=100px, m=320px, x=800px, y=1280px)
    For videos/documents: Downloads the thumbnail using thumb=-1
    """
    try:
        @sync_to_async
        def get_storage_config():
            settings = GlobalSettings.get_settings()
            return settings.storage_root, is_cloud_backend(settings), get_storage_backend(settings)

        storage_root, use_cloud, backend = await get_storage_config()

        thumb_filename = f"thumb_{message_id}.jpg"
        relative_path = f"{channel_id}/thumbnails/{thumb_filename}"

        if use_cloud:
            temp_dir = Path(tempfile.gettempdir()) / 'trawlr-downloads'
            temp_dir.mkdir(parents=True, exist_ok=True)
            thumb_path = temp_dir / thumb_filename
        else:
            thumb_dir = Path(storage_root) / str(channel_id) / 'thumbnails'
            thumb_dir.mkdir(parents=True, exist_ok=True)
            thumb_path = thumb_dir / thumb_filename
        logger.info(f"Thumbnail download path: {thumb_path}")

        downloaded = None

        if message.photo:
            # For photos, select the configured size
            # Photo sizes: s (100px), m (320px), x (800px), y (1280px), w (full)
            sizes = message.photo.sizes
            logger.info(f"Available photo sizes for {message_id}: {[getattr(s, 'type', 'unknown') for s in sizes]}")

            # Find the configured size, with fallbacks
            target_size = None
            # Try configured size first, then fallback to smaller sizes
            fallback_order = {
                's': ['s', 'm', 'x', 'y', 'w'],
                'm': ['m', 's', 'x', 'y', 'w'],
                'x': ['x', 'm', 'y', 's', 'w'],
                'y': ['y', 'x', 'm', 'w', 's'],
            }
            preferred_types = fallback_order.get(thumbnail_size, ['m', 's', 'x', 'y', 'w'])

            for preferred_type in preferred_types:
                for size in sizes:
                    if getattr(size, 'type', '') == preferred_type:
                        target_size = size
                        break
                if target_size:
                    break

            # If no preferred size found, use the last available
            if not target_size and sizes:
                target_size = sizes[-1]

            logger.info(f"Downloading photo thumbnail for {message_id} using size type: {getattr(target_size, 'type', 'unknown')} (configured: {thumbnail_size})")
            downloaded = await client.download_media(
                message,
                file=str(thumb_path),
                thumb=target_size
            )
            logger.info(f"Photo download result: {downloaded}")
        else:
            # For videos/documents, use thumb=-1 to get thumbnail
            logger.info(f"Downloading video/doc thumbnail for {message_id}")
            downloaded = await client.download_media(
                message,
                file=str(thumb_path),
                thumb=-1
            )
            logger.info(f"Video/doc thumbnail result: {downloaded}")

        if downloaded:
            exists = Path(downloaded).exists() if downloaded else False
            logger.info(f"Downloaded to {downloaded}, exists={exists}")

            # Upload to cloud storage if needed
            if use_cloud and exists:
                try:
                    @sync_to_async
                    def upload_thumbnail():
                        backend.save_file(relative_path, str(thumb_path))
                    await upload_thumbnail()
                    logger.info(f"Uploaded thumbnail to cloud: {relative_path}")
                finally:
                    try:
                        if thumb_path.exists():
                            os.remove(thumb_path)
                    except Exception:
                        pass

            return relative_path

    except Exception as e:
        logger.exception(f"Could not download thumbnail for message {message_id}: {e}")

    return ''


async def _async_create_archived_message(**kwargs):
    """Create ArchivedMessage asynchronously. Returns the created/updated message."""
    @sync_to_async
    def create():
        channel = kwargs.pop('channel')
        message_id = kwargs.pop('message_id')
        msg, _ = ArchivedMessage.objects.update_or_create(
            channel=channel,
            message_id=message_id,
            defaults=kwargs
        )
        return msg

    return await create()


# Map Telethon entity class names to our entity types
ENTITY_TYPE_MAP = {
    'MessageEntityUrl': 'url',
    'MessageEntityTextUrl': 'text_url',
    'MessageEntityMention': 'mention',
    'MessageEntityMentionName': 'mention_name',
    'InputMessageEntityMentionName': 'mention_name',
    'MessageEntityHashtag': 'hashtag',
    'MessageEntityEmail': 'email',
    'MessageEntityPhone': 'phone',
    'MessageEntityBotCommand': 'bot_command',
}

# Entity types we intentionally skip (no logging for these)
IGNORED_ENTITY_TYPES = {
    'MessageEntityCashtag',
    'MessageEntityBold',
    'MessageEntityItalic',
    'MessageEntityUnderline',
    'MessageEntityStrike',
    'MessageEntityCode',
    'MessageEntityPre',
    'MessageEntitySpoiler',
    'MessageEntityBlockquote',
    'MessageEntityCustomEmoji',
}


def _utf16_offset_to_index(text, utf16_offset):
    """
    Convert UTF-16 code unit offset to Python string index.

    Telegram entity offsets are in UTF-16 code units:
    - BMP characters (U+0000 to U+FFFF): 1 UTF-16 code unit
    - Non-BMP characters like emoji (U+10000+): 2 UTF-16 code units (surrogate pair)
    """
    utf16_pos = 0
    for i, char in enumerate(text):
        if utf16_pos >= utf16_offset:
            return i
        utf16_pos += 2 if ord(char) > 0xFFFF else 1
    return len(text)


def _extract_entities_data(text, entities):
    """
    Extract entity data from Telethon entity objects into plain Python dicts.
    This must be called from the async context BEFORE passing to sync_to_async.

    NOTE: Telegram entity offsets are in UTF-16 code units, not Python string indices.
    We must convert to handle emoji and other non-BMP characters correctly.
    The text parameter must be message.message (plain text), NOT message.text (may have formatting).
    """
    entities_data = []

    for entity in entities:
        entity_class = type(entity).__name__
        entity_type = ENTITY_TYPE_MAP.get(entity_class)

        if not entity_type:
            if entity_class not in IGNORED_ENTITY_TYPES:
                logger.debug(f"Unknown entity type: {entity_class}")
            continue

        # Extract text using UTF-16 offset conversion
        try:
            text_str = text or ''
            start_idx = _utf16_offset_to_index(text_str, entity.offset)
            end_idx = _utf16_offset_to_index(text_str, entity.offset + entity.length)
            entity_text = text_str[start_idx:end_idx]
        except Exception as e:
            logger.warning(f"Failed to extract entity text: {e}")
            entity_text = ''

        data = {
            'entity_type': entity_type,
            'offset': entity.offset,
            'length': entity.length,
            'text': entity_text,
            'url': '',
            'user_id': None,
            'language': '',
            'custom_emoji_id': None,
        }

        # Extract type-specific fields
        if entity_type == 'text_url':
            data['url'] = getattr(entity, 'url', '') or ''
        elif entity_type == 'url':
            data['url'] = entity_text
        elif entity_type == 'mention_name':
            data['user_id'] = getattr(entity, 'user_id', None)
        elif entity_type == 'pre':
            data['language'] = getattr(entity, 'language', '') or ''
        elif entity_type == 'custom_emoji':
            data['custom_emoji_id'] = getattr(entity, 'document_id', None)

        entities_data.append(data)

    return entities_data


def _extract_forward_data(forward):
    """
    Extract forward source data from Telethon forward object into a plain Python dict.
    This must be called from the async context BEFORE passing to sync_to_async.
    """
    # Determine source type and extract info
    source_type = 'hidden'
    source_telegram_id = None
    source_title = ''
    source_username = ''
    source_is_verified = False
    source_is_scam = False
    source_is_fake = False
    source_is_broadcast = False

    # Check if forward has chat info (channel/group forward)
    if forward.chat:
        chat = forward.chat
        source_type = 'channel'
        source_telegram_id = getattr(chat, 'id', None)
        source_title = getattr(chat, 'title', '') or ''
        source_username = getattr(chat, 'username', '') or ''
        source_is_verified = getattr(chat, 'verified', False) or False
        source_is_scam = getattr(chat, 'scam', False) or False
        source_is_fake = getattr(chat, 'fake', False) or False
        source_is_broadcast = getattr(chat, 'broadcast', False) or False
    elif forward.sender:
        # Forward from a user
        source_type = 'user'
        sender = forward.sender
        source_telegram_id = getattr(sender, 'id', None)
        first_name = getattr(sender, 'first_name', '') or ''
        last_name = getattr(sender, 'last_name', '') or ''
        source_title = f"{first_name} {last_name}".strip()
        source_username = getattr(sender, 'username', '') or ''
        source_is_verified = getattr(sender, 'verified', False) or False
        source_is_scam = getattr(sender, 'scam', False) or False
        source_is_fake = getattr(sender, 'fake', False) or False

    # Extract original forward header info
    original_fwd = getattr(forward, 'original_fwd', None)
    original_message_id = getattr(forward, 'channel_post', None)
    original_date = getattr(forward, 'date', None)
    original_author = getattr(forward, 'post_author', '') or ''
    from_name = getattr(forward, 'from_name', '') or ''

    # If we have original_fwd, extract from there too
    if original_fwd:
        if not original_message_id:
            original_message_id = getattr(original_fwd, 'channel_post', None)
        if not original_date:
            original_date = getattr(original_fwd, 'date', None)
        if not original_author:
            original_author = getattr(original_fwd, 'post_author', '') or ''
        if not from_name:
            from_name = getattr(original_fwd, 'from_name', '') or ''

    return {
        'source_type': source_type,
        'source_telegram_id': source_telegram_id,
        'source_title': source_title,
        'source_username': source_username,
        'original_message_id': original_message_id,
        'original_date': original_date,
        'original_author': original_author,
        'from_name': from_name,
        'source_is_verified': source_is_verified,
        'source_is_scam': source_is_scam,
        'source_is_fake': source_is_fake,
        'source_is_broadcast': source_is_broadcast,
    }


async def _async_create_message_entities(archived_msg, entities_data):
    """
    Store pre-extracted message entities (URLs, mentions, hashtags, etc.).
    entities_data should be a list of dicts from _extract_entities_data().

    Dual-write: every MessageEntity row points at a deduped GlobalEntity via
    the `entity` FK. GlobalEntity.bulk_get_or_create batches the lookup —
    0 queries if cached, 1 if all exist in DB, 3 if some are new.
    """
    @sync_to_async
    def create_entities():
        # Delete existing entities for this message (in case of update)
        MessageEntity.objects.filter(message=archived_msg).delete()

        if not entities_data:
            return

        hash_to_id = GlobalEntity.bulk_get_or_create(entities_data)

        entities_to_create = []
        for data in entities_data:
            h = GlobalEntity.compute_hash(**data)
            entities_to_create.append(MessageEntity(
                message=archived_msg,
                channel=archived_msg.channel,
                entity_id=hash_to_id[h],
                offset=data['offset'],
                length=data['length'],
            ))

        MessageEntity.objects.bulk_create(entities_to_create)

    try:
        await create_entities()
    except Exception as e:
        logger.exception(f"Failed to create message entities: {e}")


async def _async_create_forward_source(archived_msg, forward_data):
    """
    Store pre-extracted forward source information.
    forward_data should be a dict from _extract_forward_data().
    """
    @sync_to_async
    def create_forward():
        # Delete existing forward source (in case of update)
        ForwardSource.objects.filter(message=archived_msg).delete()

        ForwardSource.objects.create(
            message=archived_msg,
            source_type=forward_data['source_type'],
            source_telegram_id=forward_data['source_telegram_id'],
            source_title=forward_data['source_title'],
            source_username=forward_data['source_username'],
            original_message_id=forward_data['original_message_id'],
            original_date=forward_data['original_date'],
            original_author=forward_data['original_author'],
            from_name=forward_data['from_name'],
            source_is_verified=forward_data['source_is_verified'],
            source_is_scam=forward_data['source_is_scam'],
            source_is_fake=forward_data['source_is_fake'],
            source_is_broadcast=forward_data['source_is_broadcast'],
        )

    try:
        await create_forward()
    except Exception as e:
        logger.exception(f"Failed to create forward source: {e}")


async def _async_create_download_task(**kwargs):
    """Create DownloadTask asynchronously if it doesn't exist and not already downloaded."""
    @sync_to_async
    def create():
        channel = kwargs.pop('channel')
        message_id = kwargs.pop('message_id')

        # Check if already downloaded - don't create task if file exists
        if DownloadedFile.objects.filter(channel=channel, message_id=message_id).exists():
            return None, False

        task, created = DownloadTask.objects.get_or_create(
            channel=channel,
            message_id=message_id,
            defaults={
                **kwargs,
                'max_retries': 3,
                'pending_reason': 'queued',
            }
        )
        return task, created

    task, created = await create()

    # Broadcast queue update if a new task was created
    if created and task:
        try:
            # Get user ID from the channel's account
            user_id = task.channel.account.user.pk
            await sync_to_async(sync_broadcast_queue_update, thread_sensitive=False)(
                user_id, 'added', task_id=task.pk
            )
        except Exception as e:
            logger.debug(f"Failed to broadcast queue update: {e}")

    return created
