"""
EventPublisher - Publishes Telegram events to RabbitMQ for processing.

This module provides the thin layer between the Telegram listener and the
event processing workers. It serializes events and publishes them to a
durable queue for reliable processing.
"""

import json
import logging
import time
import uuid
from datetime import datetime
from typing import Optional

import aio_pika

from events.types import EventType, QUEUE_EVENTS

logger = logging.getLogger('trawlr.listener_service.event_publisher')

# Actor name must match the function name in events/processor.py
ACTOR_NAME = 'process_telegram_event'


class EventPublisher:
    """
    Publishes raw Telegram events to RabbitMQ for processing.

    This class handles:
    - Connection to RabbitMQ
    - Event serialization
    - Reliable message delivery with persistence
    """

    def __init__(self, rabbitmq_url: str):
        self._rabbitmq_url = rabbitmq_url
        self._connection: Optional[aio_pika.Connection] = None
        self._channel: Optional[aio_pika.Channel] = None
        self._exchange: Optional[aio_pika.Exchange] = None

    async def connect(self):
        """Connect to RabbitMQ for publishing.

        Note: We don't declare the queue here - let Dramatiq (the consumer)
        handle queue declaration with proper dead-letter settings.
        """
        logger.info("Connecting to RabbitMQ for event publishing")

        self._connection = await aio_pika.connect_robust(self._rabbitmq_url)
        self._channel = await self._connection.channel()
        self._exchange = self._channel.default_exchange
        logger.info(f"Connected to RabbitMQ, ready to publish to '{QUEUE_EVENTS}'")

    async def disconnect(self):
        """Disconnect from RabbitMQ."""
        if self._connection:
            await self._connection.close()
            self._connection = None
            self._channel = None
            self._exchange = None
            logger.info("Disconnected from RabbitMQ")

    async def publish_new_message(
        self,
        account_id: int,
        chat_id: int,
        message_id: int,
        message_data: dict,
        sender_data: Optional[dict],
        media_info: dict,
        entities_data: list,
        forward_data: Optional[dict],
        tgraw: Optional[dict] = None,
    ):
        """
        Publish a new message event.

        Args:
            account_id: ID of the TelegramAccount
            chat_id: Telegram chat ID
            message_id: Telegram message ID
            message_data: Dict with message text, flags, dates, etc.
            sender_data: Dict with sender info (or None)
            media_info: Dict with media type, file info, etc. (includes download_thumbnail, thumbnail_size if enabled)
            entities_data: List of extracted entity dicts
            forward_data: Dict with forward source info (or None)
            tgraw: Full serialized Telethon message object (or None)
        """
        payload = {
            'type': EventType.NEW_MESSAGE,
            'account_id': account_id,
            'chat_id': chat_id,
            'message_id': message_id,
            'timestamp': datetime.utcnow().isoformat(),
            'message_data': message_data,
            'sender_data': sender_data,
            'media_info': media_info,
            'entities_data': entities_data,
            'forward_data': forward_data,
        }
        if tgraw is not None:
            payload['tgraw'] = tgraw

        await self._publish(payload)
        logger.debug(f"Published new_message event: msg={message_id} chat={chat_id}")

    async def publish_message_edited(
        self,
        account_id: int,
        chat_id: int,
        message_id: int,
        message_data: dict,
        tgraw: Optional[dict] = None,
    ):
        """Publish a message edited event."""
        payload = {
            'type': EventType.MESSAGE_EDITED,
            'account_id': account_id,
            'chat_id': chat_id,
            'message_id': message_id,
            'timestamp': datetime.utcnow().isoformat(),
            'message_data': message_data,
        }
        if tgraw is not None:
            payload['tgraw'] = tgraw

        await self._publish(payload)
        logger.debug(f"Published message_edited event: msg={message_id}")

    async def publish_message_deleted(
        self,
        account_id: int,
        chat_id: int,
        message_ids: list[int],
    ):
        """Publish a message deleted event."""
        payload = {
            'type': EventType.MESSAGE_DELETED,
            'account_id': account_id,
            'chat_id': chat_id,
            'message_ids': message_ids,
            'timestamp': datetime.utcnow().isoformat(),
        }

        await self._publish(payload)
        logger.debug(f"Published message_deleted event: msgs={message_ids}")

    async def publish_chat_action(
        self,
        account_id: int,
        chat_id: int,
        action_type: str,
        user_id: Optional[int] = None,
        action_data: Optional[dict] = None,
        user_data: Optional[dict] = None,
    ):
        """Publish a chat action event (user joins/leaves, title changes, etc.)."""
        payload = {
            'type': EventType.CHAT_ACTION,
            'account_id': account_id,
            'chat_id': chat_id,
            'action_type': action_type,
            'user_id': user_id,
            'action_data': action_data or {},
            'user_data': user_data,
            'timestamp': datetime.utcnow().isoformat(),
        }

        await self._publish(payload)
        logger.debug(f"Published chat_action event: {action_type}")

    async def publish_user_update(
        self,
        account_id: int,
        user_id: int,
        user_data: dict,
    ):
        """Publish a user update event."""
        payload = {
            'type': EventType.USER_UPDATE,
            'account_id': account_id,
            'user_id': user_id,
            'user_data': user_data,
            'timestamp': datetime.utcnow().isoformat(),
        }

        await self._publish(payload)
        logger.debug(f"Published user_update event: user={user_id}")

    async def publish_channel_update(
        self,
        account_id: int,
        channel_id: int,
        raw_update: dict,
    ):
        """Publish a channel update event (UpdateChannel)."""
        payload = {
            'type': EventType.CHANNEL_UPDATE,
            'account_id': account_id,
            'chat_id': channel_id,
            'raw_update': raw_update,
            'timestamp': datetime.utcnow().isoformat(),
        }

        await self._publish(payload)
        logger.debug(f"Published channel_update event: channel={channel_id}")

    async def publish_channel_participants(
        self,
        account_id: int,
        channel_id: int,
        raw_update: dict,
    ):
        """Publish a channel participants update event (UpdateChatParticipants)."""
        payload = {
            'type': EventType.CHANNEL_PARTICIPANTS,
            'account_id': account_id,
            'chat_id': channel_id,
            'raw_update': raw_update,
            'timestamp': datetime.utcnow().isoformat(),
        }

        await self._publish(payload)
        logger.debug(f"Published channel_participants event: channel={channel_id}")

    async def publish_channel_participant(
        self,
        account_id: int,
        channel_id: int,
        user_id: int,
        raw_update: dict,
    ):
        """Publish a channel participant change event (UpdateChannelParticipant - supergroups)."""
        payload = {
            'type': EventType.CHANNEL_PARTICIPANT,
            'account_id': account_id,
            'chat_id': channel_id,
            'user_id': user_id,
            'raw_update': raw_update,
            'timestamp': datetime.utcnow().isoformat(),
        }

        await self._publish(payload)
        logger.debug(f"Published channel_participant event: channel={channel_id}, user={user_id}")

    async def publish_channel_pinned(
        self,
        account_id: int,
        channel_id: int,
        message_id: Optional[int],
        raw_update: dict,
    ):
        """Publish a channel pinned message event (UpdateChannelPinnedMessage / UpdatePinnedChannelMessages)."""
        payload = {
            'type': EventType.CHANNEL_PINNED,
            'account_id': account_id,
            'chat_id': channel_id,
            'message_id': message_id,
            'raw_update': raw_update,
            'timestamp': datetime.utcnow().isoformat(),
        }

        await self._publish(payload)
        logger.debug(f"Published channel_pinned event: channel={channel_id}")

    async def _publish(self, payload: dict):
        """Publish a payload to the queue in Dramatiq message format."""
        if not self._exchange:
            raise RuntimeError("EventPublisher not connected")

        # Generate a unique event_id (trawlr event ID) for this payload
        # This can be used to link raw events to processed data
        event_id = str(uuid.uuid4())
        payload['event_id'] = event_id

        # Format message in Dramatiq's expected format
        dramatiq_message = {
            'queue_name': QUEUE_EVENTS,
            'actor_name': ACTOR_NAME,
            'args': [payload],  # Pass payload as first positional arg
            'kwargs': {},
            'options': {},
            'message_id': event_id,  # Use same ID for Dramatiq message
            'message_timestamp': int(time.time() * 1000),
        }

        message = aio_pika.Message(
            body=json.dumps(dramatiq_message, default=str).encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,  # Survives restart
            content_type='application/json',
        )

        await self._exchange.publish(
            message,
            routing_key=QUEUE_EVENTS,
        )
