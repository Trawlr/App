"""
Events module - Processes Telegram events from the queue.

This module handles the processing of events that are queued by the listener service.
The listener service captures raw events and publishes them to RabbitMQ, while this
module's workers consume and process those events asynchronously.
"""

from .types import EventType, QUEUE_EVENTS

__all__ = ['EventType', 'QUEUE_EVENTS']
