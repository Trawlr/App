"""
Events app models for storing raw event data.
"""

import uuid

from django.db import models

from accounts.models import TelegramAccount


class RawEvents(models.Model):
    """
    Stores raw JSON event data from Telegram before processing.

    This enables debugging, auditing, and reprocessing of events.
    The event_id (trawlr event ID) links to the processed ArchivedMessage.
    """

    EVENT_TYPE_CHOICES = [
        ('new_message', 'New Message'),
        ('message_edited', 'Message Edited'),
        ('message_deleted', 'Message Deleted'),
        ('chat_action', 'Chat Action'),
        ('user_update', 'User Update'),
        # Channel-level events (raw updates)
        ('channel_update', 'Channel Update'),
        ('channel_participants', 'Channel Participants'),
        ('channel_participant', 'Channel Participant Change'),
        ('channel_pinned', 'Channel Pinned Message'),
    ]

    # Primary key - the "trawlr event ID"
    event_id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        help_text='Unique trawlr event identifier'
    )

    # Event metadata
    event_type = models.CharField(
        max_length=50,
        choices=EVENT_TYPE_CHOICES,
        db_index=True
    )
    account = models.ForeignKey(
        TelegramAccount,
        on_delete=models.CASCADE,
        related_name='raw_events',
        help_text='Account that received this event'
    )

    # Telegram identifiers
    chat_id = models.BigIntegerField(
        db_index=True,
        help_text='Telegram chat/channel ID'
    )
    message_id = models.BigIntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text='Telegram message ID (null for some event types)'
    )

    # The raw payload
    raw_json = models.JSONField(
        help_text='Complete raw event payload as received'
    )

    # Timestamps
    event_timestamp = models.DateTimeField(
        db_index=True,
        help_text='Timestamp of the event itself (from Telegram)'
    )
    received_at = models.DateTimeField(
        auto_now_add=True,
        help_text='When we received/stored this event'
    )

    class Meta:
        ordering = ['-received_at']
        verbose_name = 'Raw Event'
        verbose_name_plural = 'Raw Events'
        indexes = [
            models.Index(fields=['account', 'chat_id', 'message_id']),
            models.Index(fields=['event_type', '-event_timestamp']),
            models.Index(fields=['-received_at']),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} - {self.event_id}"
