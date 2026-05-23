"""
Downloads app models for download queue and file management.
"""

import os
import uuid

from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from simple_history.models import HistoricalRecords
from storage.utils import get_storage_backend

from audit.models import TelegramUser



class DownloadTask(models.Model):
    """A single file download task in the queue."""

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('downloading', 'Downloading'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('paused', 'Paused'),
        ('unavailable', 'Unavailable'),  # Message deleted/no media - not retriable
        ('archived', 'Completed'), 
    ]

    PENDING_REASON_CHOICES = [
        ('queued', 'Queued'),
        ('dispatched', 'Dispatched'),
        ('channel_paused', 'Channel Paused'),
        ('account_flood_wait', 'Account Flood Wait'),
        ('rate_limited', 'Rate Limited'),
        ('no_slots', 'No Available Slots'),
    ]

    FILE_TYPE_CHOICES = [
        ('photo', 'Photo'),
        ('video', 'Video'),
        ('file', 'File'),
    ]

    channel = models.ForeignKey(
        'audit.TelegramChannel',
        on_delete=models.CASCADE,
        related_name='download_tasks'
    )
    message_id = models.BigIntegerField()

    # File info from Telegram
    telegram_file_id = models.CharField(max_length=255)
    file_unique_id = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        help_text='Stable unique file ID from Telegram (same across all bots/accounts)'
    )
    original_filename = models.CharField(max_length=255, blank=True)
    file_type = models.CharField(max_length=20, choices=FILE_TYPE_CHOICES)
    file_size = models.BigIntegerField(default=0)
    mime_type = models.CharField(max_length=100, blank=True)

    # Download state
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        db_index=True
    )
    pending_reason = models.CharField(
        max_length=30,
        choices=PENDING_REASON_CHOICES,
        default='queued',
        blank=True,
        help_text='Why this task is pending (if applicable)'
    )
    priority = models.IntegerField(default=5, db_index=True)
    progress = models.IntegerField(default=0)  # 0-100 percentage
    downloaded_bytes = models.BigIntegerField(default=0)
    download_speed = models.IntegerField(default=0)  # bytes/sec

    # Retry tracking
    retry_count = models.IntegerField(default=0)
    max_retries = models.IntegerField(default=3)
    last_error = models.TextField(blank=True)

    # Dramatiq message ID for the in-flight download task

    celery_task_id = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Track all changes with django-simple-history
    history = HistoricalRecords()

    class Meta:
        ordering = ['-priority', 'created_at']
        verbose_name = 'Download Task'
        verbose_name_plural = 'Download Tasks'
        indexes = [
            models.Index(fields=['status', 'priority']),
            models.Index(fields=['channel', 'message_id']),
        ]

    def __str__(self):
        return f"{self.get_file_type_display()} from {self.channel.title} ({self.status})"

    @property
    def progress_display(self):
        """Return formatted progress string."""
        if self.file_size > 0:
            return f"{self.downloaded_bytes / (1024*1024):.1f} / {self.file_size / (1024*1024):.1f} MB"
        return f"{self.progress}%"

    @property
    def can_retry(self):
        """Check if task can be retried."""
        return self.status == 'failed' and self.retry_count < self.max_retries

    @property
    def is_retriable(self):
        """Check if task is potentially retriable (not permanently unavailable)."""
        return self.status != 'unavailable'


class DownloadedFile(models.Model):
    """A successfully downloaded file."""

    FILE_TYPE_CHOICES = [
        ('photo', 'Photo'),
        ('video', 'Video'),
        ('file', 'File'),
    ]

    channel = models.ForeignKey(
        'audit.TelegramChannel',
        on_delete=models.CASCADE,
        related_name='downloaded_files'
    )
    task = models.OneToOneField(
        DownloadTask,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='downloaded_file'
    )
    message_id = models.BigIntegerField()

    # File info
    original_filename = models.CharField(max_length=255)
    stored_filename = models.CharField(max_length=255)
    file_path = models.CharField(max_length=500)
    file_type = models.CharField(max_length=20, choices=FILE_TYPE_CHOICES)
    file_size = models.BigIntegerField()
    mime_type = models.CharField(max_length=100, blank=True)

    # Deduplication
    sha256_hash = models.CharField(max_length=64, db_index=True)
    is_duplicate = models.BooleanField(default=False)
    original_file = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='duplicates'
    )

    # Thumbnail
    thumbnail_path = models.CharField(max_length=500, blank=True)

    # Media dimensions and duration
    media_width = models.IntegerField(null=True, blank=True, help_text='Width in pixels (photos/videos)')
    media_height = models.IntegerField(null=True, blank=True, help_text='Height in pixels (photos/videos)')
    media_duration = models.IntegerField(null=True, blank=True, help_text='Duration in seconds (videos/audio)')

    # Metadata from Telegram
    telegram_file_id = models.CharField(max_length=255)
    file_unique_id = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        help_text='Stable unique file ID from Telegram - used for global deduplication'
    )
    telegram_date = models.DateTimeField()

    downloaded_at = models.DateTimeField(auto_now_add=True)

    # Manual deletion flag
    deleted_from_disk = models.BooleanField(default=False, db_default=False)

    # Track all changes with django-simple-history
    history = HistoricalRecords()

    class Meta:
        ordering = ['-downloaded_at']
        verbose_name = 'Downloaded File'
        verbose_name_plural = 'Downloaded Files'
        indexes = [
            models.Index(fields=['channel', 'file_type']),
            models.Index(fields=['sha256_hash']),
            models.Index(fields=['channel', '-downloaded_at']),
        ]

    def __str__(self):
        return self.original_filename

    @property
    def file_url(self):
        """Return URL for accessing the file via the serve_media view."""
        return reverse('downloads:serve_media', kwargs={'pk': self.pk})

    @property
    def thumbnail_url(self):
        """Return URL for accessing the thumbnail via the serve_thumbnail view."""
        if not self.thumbnail_path:
            return None
        return reverse('downloads:serve_thumbnail', kwargs={'pk': self.pk})

    def get_display_thumbnail_url(self):
        """
        Get the best thumbnail URL for display.
        Falls back to ArchivedMessage thumbnail if DownloadedFile doesn't have one.
        """
        # First check if we have a direct thumbnail
        if self.thumbnail_path:
            return self.thumbnail_url

        # Try to get thumbnail from the corresponding ArchivedMessage
        # ArchivedMessage is defined later in this file, but we can reference it directly
        archived = ArchivedMessage.objects.filter(
            channel=self.channel,
            message_id=self.message_id
        ).first()
        if archived and archived.thumbnail_path:
            return archived.thumbnail_url

        return None

    def should_use_thumbnail_for_display(self):
        """
        Check if we should use a thumbnail for display based on channel config.
        Returns True if config thumbnail_size is 'm' or larger (m, x, y).
        For photos, small thumbnails (100px) look too pixelated in cards.
        """
        try:
            config = self.channel.config
            # Use thumbnail if size is medium or larger
            return config.thumbnail_size in ['m', 'x', 'y']
        except Exception:
            return True  # Default to using thumbnails

    def delete(self, *args, **kwargs):
        """
        Custom delete that handles file deduplication relationships.

        - If this is a duplicate: just delete the record (no file to delete)
        - If this is an original with duplicates: promote one duplicate to be the new original
        - If this is an original with no duplicates: delete the file if it exists
        """
        # Import here to avoid circular imports
        from accounts.models import GlobalSettings

        if self.is_duplicate:
            # Duplicate record - no file on disk to worry about
            return super().delete(*args, **kwargs)

        # This is an original file - check for duplicates
        duplicates = self.duplicates.all()

        if duplicates.exists():
            # Promote the first duplicate to be the new original
            new_original = duplicates.first()
            new_original.is_duplicate = False
            new_original.original_file = None
            new_original.file_path = self.file_path
            new_original.stored_filename = self.stored_filename
            new_original.save(update_fields=['is_duplicate', 'original_file', 'file_path', 'stored_filename'])

            # Update remaining duplicates to point to the new original
            duplicates.exclude(pk=new_original.pk).update(original_file=new_original)

            # Delete this record without deleting the file (it's now owned by new_original)
            return super().delete(*args, **kwargs)

        # No duplicates - safe to delete the physical file
        if self.file_path and not self.deleted_from_disk:
            try:
                backend = get_storage_backend()
                backend.delete_file(self.file_path)
            except Exception:
                pass  # File may already be deleted

        return super().delete(*args, **kwargs)

    def get_actual_file(self):
        """
        Get the DownloadedFile that actually has the physical file.
        For duplicates, this returns the original file.
        For originals, returns self.
        """
        if self.is_duplicate and self.original_file:
            return self.original_file
        return self


class ArchivedMessageQuerySet(models.QuerySet):
    """Custom QuerySet for ArchivedMessage with common filters."""

    def from_active_accounts(self):
        """Return only messages from channels belonging to active accounts."""
        return self.filter(channel__account__is_active=True)


class ArchivedMessageManager(models.Manager):
    """Custom manager for ArchivedMessage."""

    def get_queryset(self):
        return ArchivedMessageQuerySet(self.model, using=self._db)

    def from_active_accounts(self):
        """Return only messages from channels belonging to active accounts."""
        return self.get_queryset().from_active_accounts()


class ArchivedMessage(models.Model):
    """Archived messages (text and media) from a channel."""

    MEDIA_TYPE_CHOICES = [
        ('photo', 'Photo'),
        ('video', 'Video'),
        ('file', 'File'),
    ]

    channel = models.ForeignKey(
        'audit.TelegramChannel',
        on_delete=models.CASCADE,
        related_name='archived_messages'
    )
    message_id = models.BigIntegerField()

    # Deduplication hash - used for unique constraint instead of message_id
    # For PeerChannel (supergroups/channels): hash of channel_id:msg:message_id
    # For PeerChat (legacy groups): hash of channel_id:chat:timestamp:sender_id:text_hash
    # This handles the case where different accounts see different message_ids for the same message
    dedup_hash = models.CharField(
        max_length=32,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text='xxhash64 for deduplication across accounts'
    )

    # Per-content hash (xxhash64 of normalized text) for cross-channel content
    # clustering. Distinct from dedup_hash, which is per-row-unique. Populated
    # only when normalized text length >= 10 chars; left null otherwise.
    content_hash = models.CharField(
        max_length=32,
        null=True,
        blank=True,
        db_index=True,
        help_text='xxhash64 of normalized text - for cross-channel CIB clustering'
    )

    # Message content
    text = models.TextField(blank=True)

    # Full-text search vector (auto-populated via database trigger)
    search_vector = SearchVectorField(null=True, blank=True)

    # Message flags
    is_pinned = models.BooleanField(default=False, help_text='Message is pinned in channel')
    is_post = models.BooleanField(default=False, help_text='Channel post (vs group message)')
    noforwards = models.BooleanField(default=False, help_text='Message cannot be forwarded')
    is_silent = models.BooleanField(default=False, help_text='Silent/no notification message')
    is_deleted = models.BooleanField(default=False, help_text='Message was deleted from source')
    deleted_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text='When the message was observed as deleted (set on the deletion event)'
    )

    # Grouped media (albums)
    grouped_id = models.BigIntegerField(
        null=True, blank=True, db_index=True,
        help_text='Groups media belonging to same album'
    )

    # Channel post metadata
    post_author = models.CharField(
        max_length=255, blank=True,
        help_text='Author signature on channel posts'
    )

    # Auto-delete
    ttl_period = models.IntegerField(
        null=True, blank=True,
        help_text='Time-to-live in seconds (disappearing message)'
    )

    # Media info
    has_media = models.BooleanField(default=False)
    media_type = models.CharField(max_length=20, choices=MEDIA_TYPE_CHOICES, blank=True)
    telegram_file_id = models.CharField(max_length=255, blank=True)
    file_unique_id = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        help_text='Stable unique file ID from Telegram - used for shared media detection'
    )
    original_filename = models.CharField(max_length=255, blank=True)
    file_size = models.BigIntegerField(default=0)
    mime_type = models.CharField(max_length=100, blank=True)
    thumbnail_path = models.CharField(max_length=500, blank=True)
    media_width = models.IntegerField(null=True, blank=True, help_text='Width in pixels (photos/videos)')
    media_height = models.IntegerField(null=True, blank=True, help_text='Height in pixels (photos/videos)')
    media_duration = models.IntegerField(null=True, blank=True, help_text='Duration in seconds (videos/audio)')
    media_unavailable = models.BooleanField(
        default=False,
        help_text='Media no longer available (deleted or edited)'
    )

    # Link to downloaded file if exists
    downloaded_file = models.ForeignKey(
        DownloadedFile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='archived_messages'
    )

    # Link to raw event data (if store_raw_events is enabled)
    raw_event = models.ForeignKey(
        'events.RawEvents',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='archived_messages',
        help_text='Raw event data that created this message'
    )

    # Sender info
    sender_id = models.BigIntegerField(null=True, blank=True)
    sender_name = models.CharField(max_length=255, blank=True)
    sender_username = models.CharField(max_length=255, blank=True)

    # Thread/reply info
    reply_to_message_id = models.BigIntegerField(null=True, blank=True)
    reply_to_top_id = models.BigIntegerField(
        null=True, blank=True, db_index=True,
        help_text='Topic ID (root message ID) when in a forum topic'
    )
    is_topic_message = models.BooleanField(
        default=False,
        help_text='True if this is a post in a topic, False if direct reply to message'
    )
    topic = models.ForeignKey(
        'audit.ForumTopic',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='messages',
        help_text='Forum topic this message belongs to'
    )

    # Engagement metrics
    views = models.IntegerField(default=0)
    forwards = models.IntegerField(default=0)
    reactions = models.JSONField(default=dict, blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    telegram_date = models.DateTimeField() # Timestamp from Telegram
    edited_date = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(auto_now_add=True)

    # Custom manager with from_active_accounts() filter
    objects = ArchivedMessageManager()

    # Track all changes with django-simple-history
    history = HistoricalRecords()

    class Meta:
        ordering = ['-telegram_date']
        verbose_name = 'Archived Message'
        verbose_name_plural = 'Archived Messages'
        # Note: unique_together on channel+message_id removed - using dedup_hash instead
        # because message_id is not globally unique in legacy groups (PeerChat)
        indexes = [
            models.Index(fields=['channel', 'has_media']),
            models.Index(fields=['channel', 'media_type']),
            models.Index(fields=['channel', 'message_id']),  # For lookups
            models.Index(fields=['-telegram_date']),  # For feed ordering
            models.Index(fields=['channel', '-telegram_date']),  # For per-channel date ordering
            GinIndex(fields=['search_vector']),  # Full-text search GIN index
            models.Index(fields=['-archived_at']),  # For realtime dashboard queries
            models.Index(fields=['channel', '-archived_at']),  # For dashboard activity queries
            models.Index(fields=['sender_id', 'telegram_date']),  # For per-user activity heatmap
        ]

    def __str__(self):
        if self.text:
            return self.text[:50] + '...' if len(self.text) > 50 else self.text
        if self.has_media:
            return f"[{self.get_media_type_display()}] {self.original_filename or self.media_type}"
        return f"Message {self.message_id}"

    @cached_property
    def _current_sender(self):
        if not self.sender_id:
            return None
        return TelegramUser.objects.filter(telegram_id=self.sender_id).only(
            'first_name', 'last_name', 'username'
        ).first()

    @property
    def effective_sender_name(self):
        """
        Display-name for this post's sender. Prefers the snapshot taken at
        archive time (the alias the user was using when they posted), falling
        back to the current TelegramUser record if no snapshot is stored.
        """
        if self.sender_name:
            return self.sender_name
        user = self._current_sender
        if user is None:
            return ''
        return f"{user.first_name} {user.last_name}".strip()

    @property
    def effective_sender_username(self):
        """
        Username for this post's sender — snapshot at post time, else current
        TelegramUser username.
        """
        if self.sender_username:
            return self.sender_username
        user = self._current_sender
        return user.username if user else ''

    @property
    def thumbnail_url(self):
        """Return URL for the thumbnail if available."""
        if not self.thumbnail_path:
            return None
        return reverse('audit:serve_post_thumbnail', kwargs={'pk': self.pk})

    @property
    def is_downloaded(self):
        """Check if the media has been fully downloaded."""
        return self.downloaded_file is not None


class TaskRun(models.Model):
    """Tracks background tasks (scans, availability checks) for duplicate prevention and cancellation."""

    TASK_TYPE_CHOICES = [
        ('scan_history', 'Scan History'),
        ('scan_members', 'Scan Members'),
        ('fetch_profiles', 'Fetch User Profiles'),
        ('check_availability', 'Check Availability'),
        ('sync_channels', 'Sync Channels'),
        ('sync_topics', 'Sync Forum Topics'),
        ('refresh_stats', 'Refresh Stats'),
        ('fetch_media_counts', 'Fetch Media Counts'),
        ('onboarding', 'Channel Onboarding'),
        # Scheduler tasks
        ('download_queue', 'Download Queue'),
        ('media_counts', 'Media Counts'),
        ('stuck_recovery', 'Stuck Recovery'),
        ('availability_check_all', 'Availability Check (All)'),
        # User actions
        ('delete_user_media', 'Delete User Media'),
    ]

    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]

    task_type = models.CharField(max_length=50, choices=TASK_TYPE_CHOICES, db_index=True)
    task_id = models.CharField(
        max_length=100,
        unique=True,
        help_text='App-level UUID to link to dramatiq task. NOT the dramatiq task id.'
    )
    channel = models.ForeignKey(
        'audit.TelegramChannel',
        on_delete=models.CASCADE,
        related_name='task_runs',
        null=True,
        blank=True,
        help_text='Associated channel (if applicable)'
    )
    account = models.ForeignKey(
        'accounts.TelegramAccount',
        on_delete=models.CASCADE,
        related_name='task_runs',
        null=True,
        blank=True,
        help_text='Associated account (if applicable)'
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='queued',
        db_index=True
    )
    should_cancel = models.BooleanField(
        default=False,
        db_index=True,
        help_text='Flag to signal task should stop'
    )

    # Progress tracking
    progress = models.JSONField(
        default=dict,
        blank=True,
        help_text='Task-specific progress data (e.g., messages_scanned, total_messages)'
    )
    progress_message = models.CharField(max_length=255, blank=True)
    progress_percent = models.IntegerField(default=0)

    # Error tracking
    error = models.TextField(blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Task Run'
        verbose_name_plural = 'Task Runs'
        indexes = [
            models.Index(fields=['task_type', 'status']),
            models.Index(fields=['channel', 'task_type', 'status']),
        ]

    def __str__(self):
        channel_name = self.channel.title if self.channel else 'N/A'
        return f"{self.get_task_type_display()} - {channel_name} ({self.status})"

    @classmethod
    def is_task_running(cls, task_type: str, channel=None, account=None) -> bool:
        """Check if a task of this type is already running for the given channel/account."""
        qs = cls.objects.filter(
            task_type=task_type,
            status__in=['queued', 'running']
        )
        if channel:
            qs = qs.filter(channel=channel)
        if account:
            qs = qs.filter(account=account)
        return qs.exists()

    @classmethod
    def get_running_task(cls, task_type: str, channel=None, account=None):
        """Get the running task of this type for the given channel/account."""
        qs = cls.objects.filter(
            task_type=task_type,
            status__in=['queued', 'running']
        )
        if channel:
            qs = qs.filter(channel=channel)
        if account:
            qs = qs.filter(account=account)
        return qs.first()

    @classmethod
    def create_task(cls, task_type: str, task_id: str, channel=None, account=None):
        """Create a new task run record."""
        return cls.objects.create(
            task_type=task_type,
            task_id=task_id,
            channel=channel,
            account=account,
            status='queued'
        )

    @classmethod
    def get_or_create_if_not_running(cls, task_type: str, channel=None, account=None):
        """
        Atomically check if a task is running and create one if not.

        Returns (task_run, created) tuple where:
        - task_run: The existing running task or the newly created one
        - created: True if a new task was created, False if one was already running

        This prevents race conditions where multiple workers check is_task_running
        simultaneously before any creates a TaskRun.
        """
        with transaction.atomic():
            # Check for existing running task with row-level lock
            existing = cls.objects.select_for_update(skip_locked=True).filter(
                task_type=task_type,
                status__in=['queued', 'running']
            )
            if channel:
                existing = existing.filter(channel=channel)
            if account:
                existing = existing.filter(account=account)

            existing_task = existing.first()
            if existing_task:
                return existing_task, False

            # No running task found, create one
            new_task = cls.objects.create(
                task_type=task_type,
                task_id=str(uuid.uuid4()),
                channel=channel,
                account=account,
                status='queued'
            )
            return new_task, True

    def mark_running(self):
        """Mark task as running."""
        self.status = 'running'
        self.started_at = timezone.now()
        self.save(update_fields=['status', 'started_at'])

    def mark_completed(self):
        """Mark task as completed."""
        self.status = 'completed'
        self.completed_at = timezone.now()
        self.progress_percent = 100
        self.save(update_fields=['status', 'completed_at', 'progress_percent'])

    def mark_failed(self, error: str = ''):
        """Mark task as failed."""
        self.status = 'failed'
        self.completed_at = timezone.now()
        self.error = error
        self.save(update_fields=['status', 'completed_at', 'error'])

    def mark_cancelled(self):
        """Mark task as cancelled."""
        self.status = 'cancelled'
        self.completed_at = timezone.now()
        self.save(update_fields=['status', 'completed_at'])

    def update_progress(self, message: str = '', percent: int = None, data: dict = None):
        """Update task progress."""
        update_fields = []
        if message:
            self.progress_message = message
            update_fields.append('progress_message')
        if percent is not None:
            self.progress_percent = percent
            update_fields.append('progress_percent')
        if data:
            self.progress.update(data)
            update_fields.append('progress')
        if update_fields:
            self.save(update_fields=update_fields)

    def request_cancel(self):
        """Request the task to cancel."""
        self.should_cancel = True
        self.save(update_fields=['should_cancel'])

    def check_cancelled(self) -> bool:
        """Check if cancellation has been requested (refreshes from DB)."""
        self.refresh_from_db(fields=['should_cancel'])
        return self.should_cancel
