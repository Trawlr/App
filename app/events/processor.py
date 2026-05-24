"""
Event processor - Dramatiq worker for processing Telegram events.

This worker consumes events from the RabbitMQ queue and processes them.
All the heavy lifting (DB queries, user tracking, entity extraction, download queueing)
happens here, not in the listener service.
"""

import trawlr.dramatiq_config  # noqa: F401

import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

import dramatiq
import pika
from django.conf import settings as django_settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from dramatiq import Retry

from accounts.models import GlobalSettings, TelegramAccount
from audit.models import (
    ActivityLog,
    ChannelConfig,
    ExclusionRule,
    ForumTopic,
    ForwardSource,
    GlobalEntity,
    MessageEntity,
    TelegramChannel,
    TelegramUser,
    TgLink,
    TgLinkEvent,
    UserGroupMembership,
)
from audit.user_tracking import track_user_from_sender_data
from downloads.consumers import sync_broadcast_queue_update
from downloads.models import ArchivedMessage, DownloadedFile, DownloadTask
from events.models import RawEvents
from tasks import dispatch_task, download_profile_photo, download_thumbnail, scan_channel_members, sync_forum_topics

from parsers import run_parsers
from parsers.tme_parser import parse_tme_link
from .types import EventType, QUEUE_EVENTS, QUEUE_RAW_EVENTS

logger = logging.getLogger('trawlr.events.processor')


_WHITESPACE_RE = re.compile(r'\s+')
CONTENT_HASH_MIN_CHARS = 10


def compute_content_hash(text: str | None) -> str | None:
    """
    Per-content hash (md5 hex) of normalized message text for cross-channel
    CIB clustering. Returns None for empty / too-short text so media-only and
    one-word posts don't pollute clusters.

    Normalization: trim, collapse internal whitespace, lowercase.
    """
    if not text:
        return None
    normalized = _WHITESPACE_RE.sub(' ', text).strip().lower()
    if len(normalized) < CONTENT_HASH_MIN_CHARS:
        return None
    return hashlib.md5(normalized.encode('utf-8')).hexdigest()


def compute_dedup_hash(channel_id: int, peer_type: str, message_id: int,
                       telegram_date: datetime | None, sender_id: int | None, text: str) -> str:
    """
    Compute deduplication hash based on peer type.

    For PeerChannel (supergroups/channels): message_id is globally unique
    For PeerChat (legacy groups): message_id differs per account, use content-based hash

    Returns:
        32-character hex string (MD5 hash)
    """
    if peer_type == 'channel':
        # Supergroups/channels: message_id is globally unique within the channel
        key = f"{channel_id}:msg:{message_id}"
    else:
        # Legacy groups (chat) or DMs (user): use content-based deduplication
        # Include timestamp (ISO format for precision), sender, and text hash
        date_str = telegram_date.isoformat() if telegram_date else ''
        text_hash = hashlib.md5(text.encode()).hexdigest()[:16] if text else ''
        key = f"{channel_id}:chat:{date_str}:{sender_id or 0}:{text_hash}"

    return hashlib.md5(key.encode()).hexdigest()


def get_processor_settings():
    """Get event processor settings from database."""
    settings = GlobalSettings.get_settings()
    return {
        'enabled': settings.event_processing_enabled,
        'batch_size': settings.event_processor_batch_size,
        'max_retries': settings.event_processor_retry_count,
        'min_backoff': settings.event_processor_retry_backoff_min * 1000,  # to ms
        'max_backoff': settings.event_processor_retry_backoff_max * 1000,
    }


def should_process_event(account_id: int | None, channel_id: int | None = None) -> tuple[bool, str]:
    """
    Check all levels of settings hierarchy to determine if event should be processed.
    Returns (should_process, reason) tuple.
    """
    # Check global settings first (doesn't need account_id)
    global_settings = GlobalSettings.get_settings()
    if not global_settings.event_processing_enabled:
        return False, 'event_processing_disabled'

    # Need account_id for further checks
    if account_id is None:
        return False, 'missing_account_id'

    # Check account settings
    try:
        account = TelegramAccount.objects.get(pk=account_id)
        if not account.process_events:
            return False, 'account_processing_disabled'
    except TelegramAccount.DoesNotExist:
        return False, 'account_not_found'

    # Check channel settings if provided
    if channel_id:
        try:
            channel = TelegramChannel.objects.select_related('config').get(pk=channel_id)
            if channel.config.bypass_listener:
                return False, 'channel_bypass_enabled'
        except TelegramChannel.DoesNotExist:
            pass  # Channel not found is OK - might be a new channel

    return True, 'ok'


def _build_chat_id_lookup(chat_id: int):
    """
    Build a list of possible chat ID variants for database lookup.

    Telegram uses multiple ID formats for channels:
    - Peer ID from events: e.g., -5107136514
    - Full channel ID: e.g., -1005107136514
    - Positive ID: e.g., 5107136514
    """
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
        lookup_ids.append(int(chat_str[4:]))           # Just the raw ID
        lookup_ids.append(-int(chat_str[4:]))          # Negative raw ID
    # Remove duplicates while preserving order
    return list(dict.fromkeys(lookup_ids))


def _store_raw_event(payload: dict):
    """
    Store raw event data if store_raw_events is enabled.

    Returns the RawEvents instance if created, None otherwise.
    """
    # Check if raw event storage is enabled
    settings = GlobalSettings.get_settings()
    if not settings.store_raw_events:
        return None

    # Get required fields
    event_id = payload.get('event_id')
    account_id = payload.get('account_id')
    event_type = payload.get('type')
    chat_id = payload.get('chat_id')
    message_id = payload.get('message_id')
    timestamp_str = payload.get('timestamp')

    # Validate required fields
    if not account_id or not event_type:
        logger.debug("Cannot store raw event - missing account_id or event_type")
        return None

    # Get account
    try:
        account = TelegramAccount.objects.get(pk=account_id)
    except TelegramAccount.DoesNotExist:
        logger.debug(f"Cannot store raw event - account {account_id} not found")
        return None

    # Parse event timestamp
    event_timestamp = timezone.now()
    if timestamp_str:
        try:
            event_timestamp = datetime.fromisoformat(timestamp_str)
        except (ValueError, TypeError):
            pass

    # Use event_id from payload or generate a new one
    if event_id:
        try:
            event_uuid = uuid.UUID(event_id)
        except (ValueError, TypeError):
            event_uuid = uuid.uuid4()
    else:
        event_uuid = uuid.uuid4()

    # Normalize chat_id: strip -100 prefix for channels/supergroups
    # to match TelegramChannel.telegram_id format
    normalized_chat_id = chat_id or 0
    if normalized_chat_id:
        chat_str = str(normalized_chat_id)
        if chat_str.startswith('-100') and len(chat_str) > 4:
            normalized_chat_id = int(chat_str[4:])  # Remove -100 prefix, keep positive

    try:
        raw_event = RawEvents.objects.create(
            event_id=event_uuid,
            event_type=event_type,
            account=account,
            chat_id=normalized_chat_id,
            message_id=message_id,
            raw_json=payload,
            event_timestamp=event_timestamp,
        )
        logger.debug(f"Stored raw event: {raw_event.event_id}")
        return raw_event
    except Exception as e:
        logger.warning(f"Failed to store raw event: {e}")
        return None


def _stream_raw_event(payload: dict):
    """
    Stream raw event to trawlr.events.raw queue if stream_raw_events is enabled.

    Uses pika to publish directly to RabbitMQ without defining an actor.
    This allows external consumers to process the raw events independently.
    """
    # Check if raw event streaming is enabled
    settings = GlobalSettings.get_settings()
    if not settings.stream_raw_events:
        return

    try:
        # Connect to RabbitMQ using the same URL as Dramatiq
        rabbitmq_url = django_settings.RABBITMQ_URL
        if rabbitmq_url.endswith('//'):
            rabbitmq_url = rabbitmq_url[:-1]

        parameters = pika.URLParameters(rabbitmq_url)
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()

        # Declare the queue (durable for persistence)
        channel.queue_declare(queue=QUEUE_RAW_EVENTS, durable=True)

        # Serialize and publish the payload
        message_body = json.dumps(payload).encode('utf-8')
        channel.basic_publish(
            exchange='',
            routing_key=QUEUE_RAW_EVENTS,
            body=message_body,
            properties=pika.BasicProperties(
                delivery_mode=2,  # Make message persistent
                content_type='application/json',
            )
        )

        connection.close()
        logger.debug(f"Streamed raw event to {QUEUE_RAW_EVENTS}")
    except Exception as e:
        logger.warning(f"Failed to stream raw event: {e}")


def _get_or_create_topic(channel, topic_id: int):
    """
    Get or create a ForumTopic record for a channel.

    If the topic doesn't exist, creates a placeholder with title "Topic {id}"
    and queues a sync task to fetch the actual topic metadata from Telegram.

    Args:
        channel: TelegramChannel instance
        topic_id: Topic ID (root message ID of the topic)

    Returns:
        ForumTopic instance or None if failed
    """
    try:
        topic, created = ForumTopic.objects.get_or_create(
            channel=channel,
            topic_id=topic_id,
            defaults={
                'title': f"Topic {topic_id}" if topic_id != 1 else "General",
                'is_general': topic_id == 1,
            }
        )
        if created:
            logger.debug(f"Created placeholder topic {topic_id} in {channel.title}")
            # Queue topic sync to fetch actual title and metadata
            _queue_topic_sync(channel.pk)
        return topic
    except Exception as e:
        logger.warning(f"Failed to get/create topic {topic_id}: {e}")
        return None


# Track recently synced channels to avoid excessive sync requests
_topic_sync_recent = {}


def _queue_topic_sync(channel_id: int):
    """
    Queue a topic sync for a channel, with rate limiting.

    Only queues if we haven't synced this channel in the last 5 minutes.
    """
    now = time.time()
    last_sync = _topic_sync_recent.get(channel_id, 0)

    # Rate limit: only sync once per 5 minutes per channel
    if now - last_sync < 300:
        logger.debug(f"Skipping topic sync for channel {channel_id} - synced recently")
        return

    _topic_sync_recent[channel_id] = now
    sync_forum_topics.send(channel_id)
    logger.debug(f"Queued topic sync for channel {channel_id}")


@dramatiq.actor(
    queue_name=QUEUE_EVENTS,
    max_retries=10000,  # High limit so events aren't lost when processing is disabled
    min_backoff=10_000,
    max_backoff=300_000,
)
def process_telegram_event(payload: dict):
    """
    Process a queued Telegram event.

    All the heavy lifting happens here - DB queries, user tracking,
    entity extraction, download queueing, etc.
    """
    account_id = payload.get('account_id')

    # Check if processing is enabled FIRST before any other work
    should_process, reason = should_process_event(account_id)
    if not should_process:
        if reason == 'event_processing_disabled':
            # Keep event in queue - will be retried when processing is re-enabled
            # Use 60s delay to avoid hammering the DB checking settings
            raise Retry(delay=60_000)
        # Other reasons (account not found, etc) - skip the event
        return

    event_type = payload.get('type')
    logger.info(f"Processing event: type={event_type}, account={account_id}")

    # Store raw event if enabled (before processing)
    raw_event = _store_raw_event(payload)

    # Stream raw event to external queue if enabled
    _stream_raw_event(payload)

    try:
        if event_type == EventType.NEW_MESSAGE:
            _process_new_message(payload, raw_event=raw_event)
        elif event_type == EventType.MESSAGE_EDITED:
            _process_message_edited(payload)
        elif event_type == EventType.MESSAGE_DELETED:
            _process_message_deleted(payload)
        elif event_type == EventType.CHAT_ACTION:
            _process_chat_action(payload)
        elif event_type == EventType.USER_UPDATE:
            _process_user_update(payload)
        elif event_type == EventType.CHANNEL_UPDATE:
            _process_channel_update(payload)
        elif event_type == EventType.CHANNEL_PARTICIPANTS:
            _process_channel_participants(payload)
        elif event_type == EventType.CHANNEL_PARTICIPANT:
            _process_channel_participant(payload)
        elif event_type == EventType.CHANNEL_PINNED:
            _process_channel_pinned(payload)
        else:
            logger.warning(f"Unknown event type: {event_type}")
    except Exception as e:
        logger.exception(f"Error processing event: {e}")
        raise  # Let Dramatiq handle retry


def _process_new_message(payload: dict, raw_event=None):
    """Process a new message event.

    Args:
        payload: Event payload dictionary
        raw_event: Optional RawEvents instance to link to the archived message

    Optimized with:
    - Transaction batching for atomicity
    - select_related to minimize queries
    - F() expressions for atomic counter updates
    - only() to fetch minimal fields where applicable
    """
    chat_id = payload.get('chat_id')
    message_id = payload.get('message_id')

    # chat_id and message_id are required to identify/store the message
    if chat_id is None or message_id is None:
        logger.warning(f"Cannot process new message - missing required fields. chat_id={chat_id}, message_id={message_id}")
        return

    # Build lookup IDs for all possible chat ID formats
    lookup_ids = _build_chat_id_lookup(chat_id)
    query = Q()
    for lid in lookup_ids:
        query |= Q(telegram_id=lid)

    # Note: telegram_id is unique across all accounts, so we don't filter by account
    # The event may come from a different account than the one that "owns" the channel
    channel = TelegramChannel.objects.select_related('config', 'account').filter(query).first()

    if not channel:
        logger.debug(f"Chat {chat_id} not tracked (lookup_ids: {lookup_ids})")
        return

    # Skip inactive sources
    if not channel.active:
        logger.debug(f"Channel {channel.title} is inactive, skipping event")
        return

    logger.info(f"Processing message {message_id} from {channel.title}")

    # Track which account received this event (for multi-account visibility)
    event_account_id = payload.get('account_id')
    if event_account_id and event_account_id != channel.account.pk:
        # Different account than the owner saw this channel - track it
        try:
            event_account = TelegramAccount.objects.get(pk=event_account_id)
            channel.seen_by_accounts.add(event_account)
        except TelegramAccount.DoesNotExist:
            pass

    # Get or create config
    try:
        config = channel.config
    except TelegramChannel.config.RelatedObjectDoesNotExist:
        config, _ = ChannelConfig.objects.get_or_create(channel=channel)

    # Check bypass_listener setting
    if config.bypass_listener:
        logger.debug(f"Bypassing listener for {channel.title}")
        return

    # Extract message data from payload
    message_data = payload.get('message_data', {})
    sender_data = payload.get('sender_data')
    sender_id = message_data.get('sender_id')

    # Track user if sender data is available
    if sender_data and sender_id:
        user, created = track_user_from_sender_data(
            sender_data,
            channel,
            message_data.get('date'),
            message_id=message_data.get('message_id'),
        )
        if created:
            _log_activity(
                'user_tracked',
                f"Tracked user: {sender_data.get('first_name', '')} (@{sender_data.get('username', '')})",
                channel=channel,
                telegram_user=user,
            )

        # Queue profile photo download if enabled (non-blocking)
        if user and channel.account.download_profile_photos and sender_data.get('photo_id'):
            # Skip if we already have this photo
            if user.photo_id != sender_data['photo_id'] or not user.profile_photo_base64:
                download_profile_photo.send(
                    channel.account.pk,
                    user.pk,
                    sender_id,
                    sender_data['photo_id']
                )
                logger.debug(f"Queued profile photo download for user {sender_id}")

    # Check exclusion list - optimized with only() to fetch minimal fields
    if sender_id:
        exclusion = ExclusionRule.objects.only('pk', 'is_global').filter(
            telegram_user__telegram_id=sender_id,
            is_active=True
        ).filter(
            Q(is_global=True) | Q(source_id=channel.pk, is_global=False)
        ).order_by('-is_global').first()

        if exclusion:
            # Atomic increment of trigger count
            ExclusionRule.objects.filter(pk=exclusion.pk).update(
                trigger_count=F('trigger_count') + 1
            )
            exclusion_type = "global" if exclusion.is_global else f"source:{channel.title}"
            logger.debug(f"Exclusion triggered ({exclusion_type}): user {sender_id}")
            return  # Drop message

    # Extract sender info
    sender_name = ''
    sender_username = ''
    if sender_data:
        sender_name = sender_data.get('first_name', '') or ''
        if sender_data.get('last_name'):
            sender_name += f" {sender_data['last_name']}"
        sender_username = sender_data.get('username', '') or ''

    # Extract media info from payload
    media_info = payload.get('media_info', {})
    has_media = media_info.get('has_media', False)
    media_type = media_info.get('media_type', '')
    file_id = media_info.get('file_id', '')
    file_unique_id = media_info.get('file_unique_id', '')
    filename = media_info.get('filename', '')
    file_size = media_info.get('file_size', 0)
    mime_type = media_info.get('mime_type', '')
    media_width = media_info.get('media_width')
    media_height = media_info.get('media_height')
    media_duration = media_info.get('media_duration')

    # Thumbnail will be downloaded by worker if enabled (not by listener)
    thumbnail_path = ''

    # Extract message flags
    is_pinned = message_data.get('is_pinned', False)
    is_post = message_data.get('is_post', False)
    noforwards = message_data.get('noforwards', False)
    is_silent = message_data.get('is_silent', False)
    grouped_id = message_data.get('grouped_id')
    post_author = message_data.get('post_author', '')
    ttl_period = message_data.get('ttl_period')

    # Extract topic/reply info
    reply_to_top_id = message_data.get('reply_to_top_id')
    is_topic_message = message_data.get('is_topic_message', False)

    # For direct topic posts, Telegram sets forum_topic=True but reply_to_top_id is null
    # In that case, the topic ID is in reply_to_message_id
    if is_topic_message and not reply_to_top_id:
        reply_to_top_id = message_data.get('reply_to_message_id')

    # Parse dates
    telegram_date = None
    if message_data.get('date'):
        telegram_date = datetime.fromisoformat(message_data['date'])

    edited_date = None
    if message_data.get('edit_date'):
        edited_date = datetime.fromisoformat(message_data['edit_date'])

    # Compute deduplication hash based on peer type
    # For PeerChannel: use message_id (globally unique)
    # For PeerChat: use content-based hash (message_id differs per account)
    peer_type = message_data.get('peer_type', 'channel')
    text = message_data.get('text', '')
    dedup_hash = compute_dedup_hash(
        channel_id=channel.telegram_id,
        peer_type=peer_type,
        message_id=message_id,
        telegram_date=telegram_date,
        sender_id=sender_id,
        text=text,
    )

    # Look up or create topic if this message belongs to a forum topic
    # reply_to_top_id indicates the topic ID for any message in a topic (both direct posts and replies)
    topic = None
    if reply_to_top_id and channel.is_forum:
        topic = _get_or_create_topic(channel, reply_to_top_id)

    # Archive message and related data in a single transaction for atomicity
    with transaction.atomic():
        # Build defaults dict
        defaults = {
            'channel': channel,
            'message_id': message_id,
            'text': text,
            'has_media': has_media,
            'media_type': media_type,
            'telegram_file_id': file_id,
            'file_unique_id': file_unique_id,
            'original_filename': filename,
            'file_size': file_size,
            'mime_type': mime_type,
            'media_width': media_width,
            'media_height': media_height,
            'media_duration': media_duration,
            'thumbnail_path': thumbnail_path,
            'sender_id': sender_id,
            'sender_name': sender_name,
            'sender_username': sender_username,
            'reply_to_message_id': message_data.get('reply_to_message_id') if not is_topic_message else None,
            'reply_to_top_id': reply_to_top_id,
            'is_topic_message': is_topic_message,
            'topic': topic,
            'views': message_data.get('views', 0),
            'forwards': message_data.get('forwards', 0),
            'reactions': message_data.get('reactions', {}),
            'telegram_date': telegram_date,
            'edited_date': edited_date,
            'content_hash': compute_content_hash(text),
            'is_pinned': is_pinned,
            'is_post': is_post,
            'noforwards': noforwards,
            'is_silent': is_silent,
            'grouped_id': grouped_id,
            'post_author': post_author,
            'ttl_period': ttl_period,
        }

        # Link to raw event if available
        if raw_event:
            defaults['raw_event'] = raw_event

        archived_msg, created = ArchivedMessage.objects.update_or_create(
            dedup_hash=dedup_hash,
            defaults=defaults,
        )

        # Extract and store message entities (inside transaction)
        entities_data = payload.get('entities_data', [])

        # Run custom parsers to extract additional entities (e.g., domains)
        additional_entities = run_parsers(payload, entities_data)
        if additional_entities:
            entities_data = entities_data + additional_entities

        if entities_data:
            _create_message_entities(archived_msg, entities_data)

        # Extract and store forward source info (inside transaction)
        forward_data = payload.get('forward_data')
        if forward_data:
            _create_forward_source(archived_msg, forward_data)

    if not created:
        logger.debug(f"Duplicate message deduplicated: hash={dedup_hash[:12]}...")

    # Queue invite link resolution for any t.me invite URLs (outside transaction)
    if created and entities_data:
        _queue_invite_link_resolution(archived_msg, entities_data, channel)

    # Log message processed (outside transaction - non-critical)
    _log_activity(
        'message_processed',
        f"Processed message {message_id}" + (f" with {media_type}" if media_type else ""),
        channel=channel,
        message_id=message_id,
        has_media=has_media,
        media_type=media_type,
    )

    # Queue thumbnail download if enabled (non-blocking)
    if media_info.get('download_thumbnail') and has_media:
        download_thumbnail.send(
            channel.account.pk,
            channel.pk,
            message_id,
            media_type,
            file_id,
            media_info.get('thumbnail_size', 'm'),
        )
        logger.debug(f"Queued thumbnail download for message {message_id}")

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
            # Check if already downloaded
            if not DownloadedFile.objects.filter(channel=channel, message_id=message_id).exists():
                effective_priority = config.priority + config.get_file_type_priority(media_type)
                task, created = DownloadTask.objects.get_or_create(
                    channel=channel,
                    message_id=message_id,
                    defaults={
                        'telegram_file_id': file_id,
                        'file_unique_id': file_unique_id,
                        'original_filename': filename,
                        'file_type': media_type,
                        'file_size': file_size,
                        'mime_type': mime_type,
                        'priority': effective_priority,
                        'max_retries': 3,
                        'pending_reason': 'queued',
                    }
                )
                if created:
                    logger.info(f"Queued download: {filename} from {channel.title}")
                    _log_activity(
                        'download_queued',
                        f"Queued {media_type}: {filename}",
                        channel=channel,
                        message_id=message_id,
                        filename=filename,
                        file_type=media_type,
                        file_size=file_size,
                    )
                    # Broadcast WebSocket update
                    _broadcast_queue_update(channel, task)


def _process_message_edited(payload: dict):
    """Process a message edited event."""
    chat_id = payload.get('chat_id')
    message_id = payload.get('message_id')

    # chat_id and message_id are required to identify the message
    if chat_id is None or message_id is None:
        logger.warning(f"Cannot process edited message - missing required fields. chat_id={chat_id}, message_id={message_id}")
        return

    logger.info(f"Processing edited message {message_id}")

    # Build lookup query for all possible chat ID formats
    lookup_ids = _build_chat_id_lookup(chat_id)
    query = Q()
    for lid in lookup_ids:
        query |= Q(channel__telegram_id=lid)

    # Find the archived message
    archived_msg = ArchivedMessage.objects.filter(
        query,
        message_id=message_id
    ).first()

    if not archived_msg:
        logger.debug(f"Edited message {message_id} not found in archive")
        return

    # Update message data
    message_data = payload.get('message_data', {})
    if message_data.get('text'):
        archived_msg.text = message_data['text']
        archived_msg.content_hash = compute_content_hash(message_data['text'])

    if message_data.get('edit_date'):
        archived_msg.edited_date = datetime.fromisoformat(message_data['edit_date'])

    # Update reactions if present
    reactions = message_data.get('reactions')
    if reactions is not None:
        archived_msg.reactions = reactions

    archived_msg.save()
    logger.info(f"Updated archived message {message_id}")


def _process_message_deleted(payload: dict):
    """Process a message deleted event."""
    chat_id = payload.get('chat_id')
    message_ids = payload.get('message_ids', [])

    logger.info(f"Processing deleted messages: {message_ids}")

    # chat_id can be None in certain Telegram scenarios (e.g., DMs, some channel types)
    # Without chat_id, we can't reliably identify which channel's messages to mark as deleted
    # since message IDs are only unique within a chat, not globally
    if chat_id is None:
        logger.warning(f"Cannot process message deletion - chat_id is None. Message IDs: {message_ids}")
        return

    # Parse event timestamp; fall back to now() if missing/malformed.
    deleted_at = timezone.now()
    ts = payload.get('timestamp')
    if ts:
        try:
            parsed = datetime.fromisoformat(ts)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt_timezone.utc)
            deleted_at = parsed
        except ValueError:
            logger.warning(f"Bad timestamp on message_deleted event: {ts!r}")

    # Build lookup query for all possible chat ID formats
    lookup_ids = _build_chat_id_lookup(chat_id)
    query = Q()
    for lid in lookup_ids:
        query |= Q(channel__telegram_id=lid)

    # Mark messages as deleted (we don't actually delete them). Only stamp deleted_at
    # on the first deletion observation — re-deletes shouldn't bump the timestamp.
    count = ArchivedMessage.objects.filter(
        query,
        message_id__in=message_ids,
        deleted_at__isnull=True,
    ).update(is_deleted=True, deleted_at=deleted_at)

    logger.info(f"Marked {count} messages as deleted")


def _process_chat_action(payload: dict):
    """Process a chat action event (user joins/leaves, title changes, etc.)."""
    action_type = payload.get('action_type')
    chat_id = payload.get('chat_id')
    user_id = payload.get('user_id')
    action_data = payload.get('action_data', {})
    user_data = payload.get('user_data')
    timestamp_str = payload.get('timestamp')

    if chat_id is None:
        logger.warning(f"Cannot process chat action - chat_id is None. Action: {action_type}")
        return

    logger.info(f"Processing chat action: {action_type} in chat {chat_id}")

    # Parse event timestamp
    event_time = timezone.now()
    if timestamp_str:
        try:
            event_time = datetime.fromisoformat(timestamp_str)
            if event_time.tzinfo is None:
                event_time = timezone.make_aware(event_time)
        except (ValueError, TypeError):
            pass

    # Find the channel using existing lookup pattern
    lookup_ids = _build_chat_id_lookup(chat_id)
    query = Q()
    for lid in lookup_ids:
        query |= Q(telegram_id=lid)

    channel = TelegramChannel.objects.filter(query).first()
    if not channel:
        logger.debug(f"Chat {chat_id} not tracked (lookup_ids: {lookup_ids})")
        return

    # Handle title changes
    if action_type == 'title_changed' and action_data.get('new_title'):
        new_title = action_data['new_title']
        old_title = channel.title
        channel.title = new_title
        channel.save(update_fields=['title'])
        logger.info(f"Updated channel title: '{old_title}' -> '{new_title}'")
        _log_activity(
            'channel_title_changed',
            f"Channel title changed from '{old_title}' to '{new_title}'",
            channel=channel,
        )
        return

    # Handle user-related actions (join/leave/kick/add)
    if user_id is None:
        logger.debug(f"No user_id for chat action: {action_type}")
        return

    # Get or create the user using user_data if available
    telegram_user = None
    user_created = False
    if user_data:
        # Use existing track_user_from_sender_data which does get_or_create
        telegram_user, user_created = track_user_from_sender_data(user_data, channel)
    else:
        # Fall back to lookup only if no user_data
        telegram_user = TelegramUser.objects.filter(telegram_id=user_id).first()

    if not telegram_user:
        logger.debug(f"User {user_id} not found and no user_data available")
        return

    # Handle join/add actions - create or update membership
    if action_type in ('user_joined', 'user_added'):
        membership, mem_created = UserGroupMembership.objects.update_or_create(
            user=telegram_user,
            channel=channel,
            defaults={
                'last_seen': event_time,
                'active': True,
                'left_at': None,
            }
        )
        if mem_created:
            membership.first_seen = event_time
            membership.save(update_fields=['first_seen'])

        if user_created:
            logger.info(f"New user {telegram_user.username or user_id} joined {channel.title}")
            _log_activity(
                'user_joined',
                f"New user {telegram_user.first_name} (@{telegram_user.username}) joined",
                channel=channel,
                telegram_user=telegram_user,
            )
        elif mem_created:
            logger.info(f"User {telegram_user.username or user_id} joined {channel.title}")
            _log_activity(
                'user_joined',
                f"User {telegram_user.first_name} (@{telegram_user.username}) joined",
                channel=channel,
                telegram_user=telegram_user,
            )
        else:
            logger.info(f"User {telegram_user.username or user_id} re-joined {channel.title}")
            _log_activity(
                'user_rejoined',
                f"User {telegram_user.first_name} (@{telegram_user.username}) re-joined",
                channel=channel,
                telegram_user=telegram_user,
            )

    # Handle leave/kick actions - mark as inactive
    elif action_type in ('user_left', 'user_kicked'):
        membership = UserGroupMembership.objects.filter(
            user=telegram_user,
            channel=channel
        ).first()
        if membership:
            membership.last_seen = event_time
            membership.active = False
            membership.left_at = event_time
            membership.save(update_fields=['last_seen', 'active', 'left_at'])

        action_verb = 'left' if action_type == 'user_left' else 'was kicked from'
        logger.info(f"User {telegram_user.username or user_id} {action_verb} {channel.title}")
        _log_activity(
            action_type,
            f"User {telegram_user.first_name} (@{telegram_user.username}) {action_verb}",
            channel=channel,
            telegram_user=telegram_user,
        )


def _process_user_update(payload: dict):
    """Process a user update event (username/name changes)."""
    user_id = payload.get('user_id')
    user_data = payload.get('user_data', {})

    logger.info(f"Processing user update for {user_id}")

    # TODO: Implement user update processing
    # - Update TelegramUser record with new data
    pass


def _process_channel_update(payload: dict):
    """
    Process a channel update event (UpdateChannel - channel metadata changes).

    Phase 1: Just log and store in RawEvents (no parsing yet).
    """
    chat_id = payload.get('chat_id')
    raw_update = payload.get('raw_update', {})

    logger.info(f"Received channel_update event: channel={chat_id}")
    logger.debug(f"Channel update raw data: {raw_update}")

    # Phase 1: No processing - event is already stored in RawEvents by _store_raw_event()
    # Phase 2 will implement: fetch updated channel info, update TelegramChannel record
    pass


# Rate limiter for member scan triggers from bulk participant updates
_member_scan_recent = {}


def _process_channel_participants(payload: dict):
    """
    Process a channel participants event (UpdateChatParticipants - bulk member changes).

    Since this event doesn't carry individual participant data, we queue a member scan
    for the affected channel to capture all changes. Rate-limited to avoid excessive scans.
    """
    chat_id = payload.get('chat_id')

    if not chat_id:
        logger.warning("Cannot process channel_participants - missing chat_id")
        return

    logger.info(f"Processing channel_participants event: channel={chat_id}")

    # Find the channel
    lookup_ids = _build_chat_id_lookup(chat_id)
    query = Q()
    for lid in lookup_ids:
        query |= Q(telegram_id=lid)

    channel = TelegramChannel.objects.filter(query).first()
    if not channel or not channel.active:
        logger.debug(f"Chat {chat_id} not tracked or inactive")
        return

    # Only scan groups/supergroups (member scanning doesn't work for channels)
    if channel.channel_type not in ('group', 'supergroup'):
        return

    # Rate limit: only queue one member scan per channel per 10 minutes
    now = time.time()
    last_scan = _member_scan_recent.get(channel.pk, 0)
    if now - last_scan < 600:
        logger.debug(f"Skipping member scan for {channel.title} - scanned recently")
        return

    _member_scan_recent[channel.pk] = now
    dispatch_task(
        scan_channel_members,
        task_type='scan_members',
        channel=channel,
        account=channel.account,
        args=(channel.pk,),
        kwargs={'skip_profile_photos': True},
    )
    logger.info(f"Queued member scan for {channel.title} (bulk participant change detected)")
    _log_activity(
        'member_scan_queued',
        f"Queued member scan for {channel.title} (participant list change detected)",
        channel=channel,
    )


def _process_channel_participant(payload: dict):
    """
    Process a channel participant event (UpdateChannelParticipant - individual member change in supergroups).

    Tracks user join/leave/promote/ban/kick by interpreting the transition between
    prev_participant_type and new_participant_type.
    """
    chat_id = payload.get('chat_id')
    user_id = payload.get('user_id')
    raw_update = payload.get('raw_update', {})

    if not chat_id or not user_id:
        logger.warning(f"Cannot process channel_participant - missing chat_id or user_id")
        return

    logger.info(f"Processing channel_participant event: channel={chat_id}, user={user_id}")

    # Find the channel
    lookup_ids = _build_chat_id_lookup(chat_id)
    query = Q()
    for lid in lookup_ids:
        query |= Q(telegram_id=lid)

    channel = TelegramChannel.objects.filter(query).first()
    if not channel:
        logger.debug(f"Chat {chat_id} not tracked (lookup_ids: {lookup_ids})")
        return

    if not channel.active:
        return

    # Parse event timestamp
    event_time = timezone.now()
    date_str = raw_update.get('date')
    if date_str:
        try:
            event_time = datetime.fromisoformat(date_str)
            if event_time.tzinfo is None:
                event_time = timezone.make_aware(event_time)
        except (ValueError, TypeError):
            pass

    # Determine what happened based on participant type transition
    prev_type = raw_update.get('prev_participant_type')
    new_type = raw_update.get('new_participant_type')

    # Participant types that indicate active membership
    MEMBER_TYPES = {
        'ChannelParticipant', 'ChannelParticipantSelf',
        'ChannelParticipantCreator', 'ChannelParticipantAdmin',
    }

    is_now_member = new_type in MEMBER_TYPES
    is_now_admin = new_type in ('ChannelParticipantAdmin', 'ChannelParticipantCreator')
    is_now_creator = new_type == 'ChannelParticipantCreator'

    # Get or create user — minimal record if new (will be enriched by future messages/scans)
    user, user_created = TelegramUser.objects.get_or_create(
        telegram_id=user_id,
        defaults={'first_name': '', 'username': ''}
    )

    if is_now_member:
        # User joined, was promoted, or had role changed
        membership, mem_created = UserGroupMembership.objects.update_or_create(
            user=user,
            channel=channel,
            defaults={
                'last_seen': event_time,
                'active': True,
                'is_admin': is_now_admin,
                'is_creator': is_now_creator,
                'left_at': None,
            }
        )
        if mem_created:
            membership.first_seen = event_time
            membership.save(update_fields=['first_seen'])

        was_gone = not prev_type or prev_type in ('ChannelParticipantBanned', 'ChannelParticipantLeft', 'NoneType')
        action = 'joined' if was_gone else 'role_changed'
        logger.info(f"User {user_id} {action} in {channel.title} (prev={prev_type}, new={new_type})")
        _log_activity(
            f'user_{action}',
            f"User {user.display_name or user_id} {action} (participant update)",
            channel=channel,
            telegram_user=user,
        )
    else:
        # User left, was banned, or was kicked
        membership = UserGroupMembership.objects.filter(
            user=user, channel=channel
        ).first()
        if membership:
            membership.active = False
            membership.left_at = event_time
            membership.last_seen = event_time
            membership.save(update_fields=['active', 'left_at', 'last_seen'])

        logger.info(f"User {user_id} removed from {channel.title} (prev={prev_type}, new={new_type})")
        _log_activity(
            'user_left',
            f"User {user.display_name or user_id} left/removed (participant update)",
            channel=channel,
            telegram_user=user,
        )


def _process_channel_pinned(payload: dict):
    """
    Process a channel pinned message event (UpdatePinnedChannelMessages).

    Phase 1: Just log and store in RawEvents (no parsing yet).
    """
    chat_id = payload.get('chat_id')
    message_id = payload.get('message_id')
    raw_update = payload.get('raw_update', {})

    logger.info(f"Received channel_pinned event: channel={chat_id}, message={message_id}")
    logger.debug(f"Channel pinned raw data: {raw_update}")

    # Phase 1: No processing - event is already stored in RawEvents by _store_raw_event()
    # Phase 2 will implement: update ArchivedMessage.is_pinned, track pin/unpin history
    pass


def _create_message_entities(archived_msg, entities_data: list):
    """Store message entities (URLs, mentions, hashtags, etc.).

    Dual-write: each MessageEntity is linked to a deduped GlobalEntity via
    the `entity` FK. Batched lookup via GlobalEntity.bulk_resolve (returns
    the freshly-inserted content_hashes for new_entity-mode notifications).
    """
    # Delete existing entities for this message (in case of update)
    MessageEntity.objects.filter(message=archived_msg).delete()

    if not entities_data:
        return

    # URL truncation must happen before hashing so dedup matches what gets stored
    # on the GlobalEntity row.
    normalized = []
    for data in entities_data:
        d = dict(data)
        d['url'] = (d.get('url', '') or '')[:2000]
        normalized.append(d)

    result = GlobalEntity.bulk_resolve(normalized)
    hash_to_id = result.hash_to_id
    newly_created_hashes = result.newly_created_hashes

    entities_to_create = []
    for data in normalized:
        h = GlobalEntity.compute_hash(**data)
        entities_to_create.append(MessageEntity(
            message=archived_msg,
            channel=archived_msg.channel,
            entity_id=hash_to_id[h],
            offset=data['offset'],
            length=data['length'],
        ))

    MessageEntity.objects.bulk_create(entities_to_create)

    # Fire watchlist matches (no-op if no active rules). Import locally so the
    # notifications app stays optional during early bootstrap and tests.
    try:
        from notifications.matcher import evaluate as _evaluate_watchlist
        _evaluate_watchlist(
            message_entities=entities_to_create,
            archived_msg=archived_msg,
            newly_created_hashes=newly_created_hashes,
        )
    except Exception as e:  # pragma: no cover - notifications must never break ingestion
        logger.warning("Watchlist matcher failed for message %s: %s", archived_msg.message_id, e)


def _create_forward_source(archived_msg, forward_data: dict):
    """Store forward source information."""
    # Delete existing forward source (in case of update)
    ForwardSource.objects.filter(message=archived_msg).delete()

    # Parse original_date if present
    original_date = None
    if forward_data.get('original_date'):
        original_date = datetime.fromisoformat(forward_data['original_date'])

    ForwardSource.objects.create(
        message=archived_msg,
        source_type=forward_data['source_type'],
        source_telegram_id=forward_data['source_telegram_id'],
        source_title=forward_data['source_title'],
        source_username=forward_data['source_username'],
        original_message_id=forward_data['original_message_id'],
        original_date=original_date,
        original_author=forward_data['original_author'],
        from_name=forward_data['from_name'],
        source_is_verified=forward_data['source_is_verified'],
        source_is_scam=forward_data['source_is_scam'],
        source_is_fake=forward_data['source_is_fake'],
        source_is_broadcast=forward_data['source_is_broadcast'],
    )


def _queue_invite_link_resolution(archived_msg, entities_data: list, channel):
    """
    Detect t.me invite URLs in message entities and queue resolution tasks.
    Deduplicates against recently resolved hashes to minimise API calls.
    """
    # Check global setting
    settings = GlobalSettings.get_settings()
    if not settings.tglink_resolution:
        return

    needs_dispatch = False

    for entity_data in entities_data:
        entity_type = entity_data.get('entity_type', '')
        if entity_type not in ('url', 'text_url'):
            continue

        url = entity_data.get('url') or entity_data.get('text', '')
        if not url:
            continue

        parsed = parse_tme_link(url)
        if not parsed or parsed['link_type'] != 'invite':
            continue

        invite_hash = parsed['identifier']

        # Find the MessageEntity record that was just created
        entity = MessageEntity.objects.filter(
            message=archived_msg,
            entity_type=entity_type,
            offset=entity_data['offset'],
        ).first()
        if not entity:
            logger.debug(f"Could not find MessageEntity for invite link at offset {entity_data['offset']}")
            continue

        # Check if this exact entity+hash sighting already exists
        if TgLinkEvent.objects.filter(entity=entity, invite_hash=invite_hash).exists():
            continue

        # Check if this hash was already resolved recently (dedup — avoid redundant API calls)
        recently_resolved = TgLinkEvent.objects.filter(
            invite_hash=invite_hash,
            source_link__isnull=False,
            resolved_at__gte=timezone.now() - timedelta(hours=24),
        ).select_related('source_link').first()

        if recently_resolved:
            # Link to existing TgLink without dispatching a task
            TgLinkEvent.objects.create(
                source_link=recently_resolved.source_link,
                entity=entity,
                channel=channel,
                invite_hash=invite_hash,
                raw_url=parsed['url'],
                resolved_at=timezone.now(),
                resolution_status='resolved',
            )
            logger.debug(
                f"Invite hash {invite_hash[:12]} already resolved recently, "
                f"linked to TgLink {recently_resolved.source_link_id}"
            )
            continue

        # Create pending event — batch task will pick it up
        TgLinkEvent.objects.create(
            entity=entity,
            channel=channel,
            invite_hash=invite_hash,
            raw_url=parsed['url'],
            resolution_status='pending',
        )
        needs_dispatch = True
        logger.debug(f"Created pending TgLinkEvent for {invite_hash[:12]}")

    # Dispatch one batch resolution task per account (if any new pending events were created)
    if needs_dispatch and hasattr(channel, 'account_id') and channel.account_id:
        from tasks.invite_links import resolve_pending_invite_links
        resolve_pending_invite_links.send_with_options(
            args=(channel.account_id,),
            delay=10_000,  # 10s delay to allow more events to accumulate
        )
        logger.debug(f"Queued batch invite link resolution for account {channel.account_id}")


def _log_activity(activity_type: str, description: str, source: str = 'worker_events', **details):
    """Log an activity to the ActivityLog table."""
    try:
        channel = details.pop('channel', None)
        telegram_user = details.pop('telegram_user', None)
        ActivityLog.log(
            activity_type=activity_type,
            description=description,
            source=source,
            channel=channel,
            telegram_user=telegram_user,
            **details
        )
    except Exception as e:
        logger.debug(f"Failed to log activity: {e}")


def _broadcast_queue_update(channel, task):
    """Broadcast WebSocket update for new download task."""
    try:
        user_id = channel.account.user.pk
        sync_broadcast_queue_update(user_id, 'added', task_id=task.pk)
    except Exception as e:
        logger.debug(f"Failed to broadcast queue update: {e}")
