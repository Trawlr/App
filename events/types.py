"""
Event type definitions for the Telegram event processing system.
"""

from enum import Enum

# Queue name for Telegram events
QUEUE_EVENTS = 'trawlr.events.telegram'

# Queue name for raw event streaming (external processing)
QUEUE_RAW_EVENTS = 'trawlr.events.raw'


class EventType(str, Enum):
    """Types of Telegram events that can be captured and processed."""

    # Message events
    NEW_MESSAGE = 'new_message'
    MESSAGE_EDITED = 'message_edited'
    MESSAGE_DELETED = 'message_deleted'

    # Chat events
    CHAT_ACTION = 'chat_action'  # User joins/leaves, title changes

    # User events
    USER_UPDATE = 'user_update'  # Username/name changes

    # Channel-level events (raw updates from MTProto)
    CHANNEL_UPDATE = 'channel_update'  # Channel metadata changes
    CHANNEL_PARTICIPANTS = 'channel_participants'  # Participant list changes (basic groups)
    CHANNEL_PARTICIPANT = 'channel_participant'  # Individual participant change (supergroups)
    CHANNEL_PINNED = 'channel_pinned'  # Pinned message changes

    def __str__(self):
        return self.value
