"""
WebSocket consumers for real-time download progress.
"""

import asyncio
import json
import logging
import threading
import time

import msgpack
import pika
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings

from .models import DownloadTask

logger = logging.getLogger('trawlr.downloads')


class DownloadProgressConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for real-time download progress updates."""

    async def connect(self):
        """Handle WebSocket connection."""
        try:
            user = self.scope.get('user')
            logger.info(f"WebSocket connect attempt, user={user}, authenticated={getattr(user, 'is_authenticated', False)}")

            if not user or not user.is_authenticated:
                logger.warning("WebSocket rejected: user not authenticated")
                await self.close()
                return

            # Join user's download progress group
            self.group_name = f"downloads_{user.id}"
            try:
                await asyncio.wait_for(
                    self.channel_layer.group_add(
                        self.group_name,
                        self.channel_name
                    ),
                    timeout=3.0
                )
            except asyncio.TimeoutError:
                logger.error(f"Timeout joining channel group - RabbitMQ connection issue")
                await self.close()
                return
            except Exception as e:
                logger.error(f"Failed to join channel group: {e}")
                await self.close()
                return

            await self.accept()
            logger.info(f"WebSocket connected for user {user.id}")
        except Exception as e:
            logger.error(f"WebSocket connect error: {e}", exc_info=True)
            await self.close()

    async def disconnect(self, close_code):
        """Handle WebSocket disconnection."""
        logger.info(f"WebSocket disconnecting with code {close_code}")
        if hasattr(self, 'group_name'):
            try:
                await asyncio.wait_for(
                    self.channel_layer.group_discard(
                        self.group_name,
                        self.channel_name
                    ),
                    timeout=2.0
                )
            except asyncio.TimeoutError:
                logger.warning("Timeout leaving channel group")
            except Exception as e:
                logger.error(f"Failed to leave channel group: {e}")

    async def receive(self, text_data):
        """Handle messages from client."""
        try:
            data = json.loads(text_data)
            action = data.get('action')

            if action == 'get_status':
                # Send current download status
                await self.send_queue_status()
            elif action == 'ping':
                await self.send(text_data=json.dumps({'type': 'pong'}))

        except json.JSONDecodeError:
            logger.warning("Invalid JSON received via WebSocket")

    async def send_queue_status(self):
        """Send current queue status to client."""
        stats = await self.get_queue_stats()
        await self.send(text_data=json.dumps({
            'type': 'queue_status',
            'stats': stats
        }))

    @database_sync_to_async
    def get_queue_stats(self):
        """Get current download queue stats."""
        user = self.scope.get('user')
        base_qs = DownloadTask.objects.filter(channel__account__user=user)

        return {
            'active': base_qs.filter(status__in=['pending', 'downloading']).count(),
            'paused': base_qs.filter(status='paused').count(),
            'completed': base_qs.filter(status__in=['completed', 'unavailable']).count(),
            'failed': base_qs.filter(status='failed').count(),
        }

    # Event handlers - called when messages are sent to the group

    async def download_progress(self, event):
        """
        Send progress update to client.
        """
        # logger.debug(f"Received progress event: task_id={event.get('task_id')}, progress={event.get('progress')}")
        await self.send(text_data=json.dumps({
            'type': 'progress',
            'task_id': event.get('task_id'),
            'progress': event.get('progress', 0),
            'downloaded_bytes': event.get('downloaded_bytes', 0),
            'file_size': event.get('file_size', 0),
            'speed': event.get('speed', 0),
            'status': event.get('status', 'downloading'),
        }))

    async def download_status_changed(self, event):
        """
        Send status change notification.

        Event format:
        {
            'type': 'download_status_changed',
            'task_id': 123,
            'status': 'completed',
            'filename': 'video.mp4',
        }
        """
        await self.send(text_data=json.dumps({
            'type': 'status_changed',
            'task_id': event.get('task_id'),
            'status': event.get('status'),
            'filename': event.get('filename', ''),
            'error': event.get('error', ''),
        }))

    async def queue_update(self, event):
        """
        Send queue state changes
        """
        await self.send(text_data=json.dumps({
            'type': 'queue_update',
            'action': event.get('action'),
            'task_id': event.get('task_id'),
            'stats': event.get('stats', {}),
        }))

    async def flood_wait(self, event):
        """
        Send flood wait notification.

        """
        await self.send(text_data=json.dumps({
            'type': 'flood_wait',
            'account_id': event.get('account_id'),
            'wait_until': event.get('wait_until'),
            'seconds': event.get('seconds', 0),
        }))


# Helper functions to broadcast messages from Dramatiq tasks

def get_channel_layer():
    """Get the channel layer for sending messages."""
    from channels.layers import get_channel_layer
    return get_channel_layer()


async def broadcast_progress(user_id: int, task_id: int, progress: int,
                             downloaded_bytes: int, file_size: int, speed: int):
    """Broadcast download progress to user's WebSocket."""
    channel_layer = get_channel_layer()
    if channel_layer:
        await channel_layer.group_send(
            f"downloads_{user_id}",
            {
                'type': 'download_progress',
                'task_id': task_id,
                'progress': progress,
                'downloaded_bytes': downloaded_bytes,
                'file_size': file_size,
                'speed': speed,
                'status': 'downloading',
            }
        )


async def broadcast_status_change(user_id: int, task_id: int, status: str,
                                  filename: str = '', error: str = ''):
    """Broadcast status change to user's WebSocket."""
    channel_layer = get_channel_layer()
    if channel_layer:
        await channel_layer.group_send(
            f"downloads_{user_id}",
            {
                'type': 'download_status_changed',
                'task_id': task_id,
                'status': status,
                'filename': filename,
                'error': error,
            }
        )


async def broadcast_queue_update(user_id: int, action: str, task_id: int = None, stats: dict = None):
    """Broadcast queue update to user's WebSocket."""
    channel_layer = get_channel_layer()
    if channel_layer:
        await channel_layer.group_send(
            f"downloads_{user_id}",
            {
                'type': 'queue_update',
                'action': action,
                'task_id': task_id,
                'stats': stats or {},
            }
        )


async def broadcast_flood_wait(user_id: int, account_id: int, wait_until: str, seconds: int):
    """Broadcast flood wait notification to user's WebSocket."""
    channel_layer = get_channel_layer()
    if channel_layer:
        await channel_layer.group_send(
            f"downloads_{user_id}",
            {
                'type': 'flood_wait',
                'account_id': account_id,
                'wait_until': wait_until,
                'seconds': seconds,
            }
        )

def _get_rabbitmq_url():
    """Get RabbitMQ URL with proper vhost encoding."""
    return settings.RABBITMQ_URL_CHANNELS


def _publish_to_group(group_name: str, message: dict):
    """
    Publish a message directly to RabbitMQ for the channels layer to pick up.
    This bypasses channels_rabbitmq's connection pool which has event loop issues.
    Uses pika (sync) to avoid event loop conflicts with Dramatiq tasks.
    """
    url = _get_rabbitmq_url()
    start_time = time.time()

    # Parse the AMQP URL and set short timeouts to avoid blocking HTTP requests
    params = pika.URLParameters(url)
    params.socket_timeout = 5  # 5 second socket timeout
    params.blocked_connection_timeout = 5  # 5 second blocked connection timeout
    connection = pika.BlockingConnection(params)
    try:
        channel = connection.channel()

        # channels_rabbitmq uses a 'groups' direct exchange with group name as routing key
        # Message format requires __asgi_group__ field
        augmented_message = dict(message)
        augmented_message['__asgi_group__'] = group_name

        body = msgpack.packb(augmented_message, use_bin_type=True)
        channel.basic_publish(
            exchange='groups',
            routing_key=group_name,
            body=body,
        )
        elapsed = (time.time() - start_time) * 1000
        if elapsed > 100:  # Log if publish takes more than 100ms
            logger.warning(f"Slow RabbitMQ publish: {elapsed:.0f}ms for {group_name}")
    finally:
        connection.close()


def sync_broadcast_progress(user_id: int, task_id: int, progress: int,
                            downloaded_bytes: int, file_size: int, speed: int):
    """Synchronous wrapper for broadcast_progress - publishes directly to RabbitMQ."""
    try:
        _publish_to_group(
            f"downloads_{user_id}",
            {
                'type': 'download_progress',
                'task_id': task_id,
                'progress': progress,
                'downloaded_bytes': downloaded_bytes,
                'file_size': file_size,
                'speed': speed,
                'status': 'downloading',
            }
        )
    except Exception as e:
        logger.debug(f"Failed to broadcast progress: {e}")


def sync_broadcast_status_change(user_id: int, task_id: int, status: str,
                                 filename: str = '', error: str = ''):
    """Synchronous wrapper for broadcast_status_change - publishes directly to RabbitMQ."""
    try:
        _publish_to_group(
            f"downloads_{user_id}",
            {
                'type': 'download_status_changed',
                'task_id': task_id,
                'status': status,
                'filename': filename,
                'error': error,
            }
        )
    except Exception as e:
        logger.debug(f"Failed to broadcast status change: {e}")


def sync_broadcast_queue_update(user_id: int, action: str, task_id: int = None, stats: dict = None):
    """
    Synchronous wrapper for broadcast_queue_update - publishes directly to RabbitMQ.
    Runs in a daemon thread to avoid blocking HTTP requests if RabbitMQ is slow/down.
    """
    def _broadcast():
        try:
            _publish_to_group(
                f"downloads_{user_id}",
                {
                    'type': 'queue_update',
                    'action': action,
                    'task_id': task_id,
                    'stats': stats or {},
                }
            )
        except Exception as e:
            logger.debug(f"Failed to broadcast queue update: {e}")

    thread = threading.Thread(target=_broadcast, daemon=True)
    thread.start()
