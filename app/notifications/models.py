"""
Entity notification engine models.

A WatchlistEntry describes a (entity_type, entity_value) pair the operator
wants to be alerted about. When the event processor sees a matching
MessageEntity it creates a NotificationDelivery row (always-on audit trail)
and dispatches it to the notifications worker for external delivery via
webhook or RabbitMQ.
"""

from django.db import models
from django.utils import timezone
from audit.models import MessageEntity


MODE_EVERY = 'all'
MODE_NEW = 'new'

MODE_CHOICES = [
    (MODE_EVERY, 'Every occurrence'),
    (MODE_NEW, 'New occurrence (first time only)'),
]

TARGET_WEBHOOK = 'webhook'
TARGET_RABBITMQ = 'rabbitmq'

TARGET_CHOICES = [
    (TARGET_WEBHOOK, 'Webhook (HTTP POST)'),
    (TARGET_RABBITMQ, 'RabbitMQ queue'),
]

STATUS_PENDING = 'pending'
STATUS_DELIVERED = 'delivered'
STATUS_FAILED = 'failed'
STATUS_EXHAUSTED = 'exhausted'

STATUS_CHOICES = [
    (STATUS_PENDING, 'Pending'),
    (STATUS_DELIVERED, 'Delivered'),
    (STATUS_FAILED, 'Failed (will retry)'),
    (STATUS_EXHAUSTED, 'Exhausted (no more retries)'),
]


class WatchlistEntry(models.Model):
    """
    A single notification rule. Watches a specific (entity_type, entity_value)
    pair and fires either every time it is seen ('every') or only the first
    time ('new').
    """

    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default=MODE_EVERY)

    entity_type = models.CharField(
        max_length=20,
        choices=MessageEntity.ENTITY_TYPE_CHOICES,
        help_text='Which kind of entity to watch (URL, hashtag, @mention, etc.).',
    )
    entity_value = models.CharField(
        max_length=2000,
        help_text=(
            'The exact value to match against, lowercased. '
            'For URLs use the full URL; for hashtags/mentions include the leading # or @.'
        ),
    )

    target_type = models.CharField(max_length=20, choices=TARGET_CHOICES)
    target_config = models.JSONField(
        default=dict,
        help_text=(
            'Webhook: {"url": "...", "secret": "...", "headers": {...}}. '
            'RabbitMQ: {"queue": "...", "exchange": "", "routing_key": "...", "declare": false}.'
        ),
    )

    cooldown_seconds = models.PositiveIntegerField(
        default=0,
        help_text='0 = fire on every occurrence. >0 suppresses additional matches within the window.',
    )

    trigger_count = models.PositiveBigIntegerField(default=0)
    last_triggered_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Watchlist Entry'
        verbose_name_plural = 'Watchlist Entries'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['is_active', 'mode']),
            models.Index(fields=['entity_type', 'entity_value']),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_mode_display()} → {self.get_target_type_display()})"

    def save(self, *args, **kwargs):
        if self.entity_value:
            self.entity_value = self.entity_value.lower()
        super().save(*args, **kwargs)

    def in_cooldown(self) -> bool:
        """Return True if a recent trigger means this entry should be suppressed."""
        if self.cooldown_seconds == 0 or self.last_triggered_at is None:
            return False
        delta = (timezone.now() - self.last_triggered_at).total_seconds()
        return delta < self.cooldown_seconds


class NotificationDelivery(models.Model):
    """
    A single attempted external delivery. One row per match-that-was-dispatched.
    Retries update the same row; on exhaustion status flips to 'exhausted'.
    """

    entry = models.ForeignKey(
        WatchlistEntry,
        on_delete=models.CASCADE,
        related_name='deliveries',
    )
    event_payload = models.JSONField(
        help_text='Snapshot of the JSON body that will be POSTed / published.',
    )
    match_context = models.JSONField(
        default=dict,
        blank=True,
        help_text='Operator-facing summary: mode, entity_type, entity_value, message_id, channel_id.',
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    attempts = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = 'Notification Delivery'
        verbose_name_plural = 'Notification Deliveries'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'last_attempt_at']),
            models.Index(fields=['entry', '-created_at']),
        ]

    def __str__(self):
        return f"Delivery {self.pk} ({self.status}) for entry {self.entry_id}"
