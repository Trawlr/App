"""
Accounts app models.
"""

from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords


class TelegramAccountQuerySet(models.QuerySet):
    """Custom QuerySet for TelegramAccount with common filters."""

    def active(self):
        """Return only active accounts."""
        return self.filter(is_active=True)

    def authenticated(self):
        """Return only authenticated accounts."""
        return self.filter(is_authenticated=True)

    def active_and_authenticated(self):
        """Return only active and authenticated accounts."""
        return self.filter(is_active=True, is_authenticated=True)


class TelegramAccountManager(models.Manager):
    """Custom manager for TelegramAccount."""

    def get_queryset(self):
        return TelegramAccountQuerySet(self.model, using=self._db)

    def active(self):
        """Return only active accounts."""
        return self.get_queryset().active()

    def authenticated(self):
        """Return only authenticated accounts."""
        return self.get_queryset().authenticated()

    def active_and_authenticated(self):
        """Return only active and authenticated accounts."""
        return self.get_queryset().active_and_authenticated()


class TelegramAccount(models.Model):
    """A Telegram account added by the user."""

    LISTENER_STATUS_CHOICES = [
        ('stopped', 'Stopped'),
        ('running', 'Running'),
        ('error', 'Error'),
        ('flood_wait', 'Flood Wait'),
    ]

    LISTENER_MODE_CHOICES = [
        ('service', 'Dedicated Service'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='telegram_accounts')
    phone_number = models.CharField(max_length=20, unique=True)
    display_name = models.CharField(max_length=100, blank=True, help_text="Friendly name for this account (defaults to phone number)")
    api_id = models.CharField(max_length=20)
    api_hash = models.CharField(max_length=64)
    session_file = models.CharField(max_length=255, blank=True)  # Deprecated, use session_string
    session_string = models.TextField(blank=True)  # Telethon StringSession stored in PostgreSQL

    is_active = models.BooleanField(default=True)
    is_authenticated = models.BooleanField(default=False)
    two_factor_enabled = models.BooleanField(default=False)

    # Listener status
    listener_status = models.CharField(
        max_length=20,
        choices=LISTENER_STATUS_CHOICES,
        default='stopped'
    )
    listener_mode = models.CharField(
        max_length=20,
        choices=LISTENER_MODE_CHOICES,
        default='service',
        help_text='Which system manages this account listener'
    )
    listener_started_at = models.DateTimeField(null=True, blank=True)
    listener_error = models.TextField(blank=True)

    # Rate limiting - when flood wait expires
    flood_wait_until = models.DateTimeField(null=True, blank=True)

    # Settings
    max_concurrent_downloads = models.IntegerField(
        default=10,
        validators=[MinValueValidator(1), MaxValueValidator(20)]
    )
    download_profile_photos = models.BooleanField(
        default=True,
        help_text='Download profile photos when scanning members or tracking new users'
    )

    # Event processing control
    process_events = models.BooleanField(
        default=True,
        help_text='If disabled, events from this account will be queued but not processed'
    )

    # Pool stats (updated by the download worker's client pool)
    pool_clients = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Custom manager with active() filter
    objects = TelegramAccountManager()

    # Track all changes with django-simple-history
    history = HistoricalRecords()

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Telegram Account'
        verbose_name_plural = 'Telegram Accounts'

    def __str__(self):
        return self.name

    @property
    def name(self):
        """Return display_name if set, otherwise phone_number."""
        return self.display_name or self.phone_number

    @property
    def is_flood_wait_active(self):
        """Check if this account is currently in flood wait."""
        if self.flood_wait_until:
            return self.flood_wait_until > timezone.now()
        return False


class GlobalSettings(models.Model):
    """Singleton model for instance-wide settings - editable via web UI."""

    FILENAME_FORMAT_CHOICES = [
        ('guid', 'GUID.jpg'),
        ('timestamp', 'timestamp_originalname.jpg'),
        ('message_id', 'messageid_originalname.jpg'),
        ('file_id', 'fileid.jpg'),
    ]

    # Interval choices in seconds (0 = disabled)
    SCHEDULER_INTERVAL_CHOICES = [
        (0, 'Disabled'),
        (5, '5 seconds'),
        (10, '10 seconds'),
        (15, '15 seconds'),
        (20, '20 seconds'),
        (30, '30 seconds'),
        (45, '45 seconds'),
        (60, '1 minute'),
        (180, '3 minutes'),
        (300, '5 minutes'),
        (600, '10 minutes'),
        (900, '15 minutes'),
        (1800, '30 minutes'),
        (2700, '45 minutes'),
        (3600, '1 hour'),
        (21600, '6 hours'),
        (43200, '12 hours'),
        (86400, '24 hours'),
    ]

    # Storage settings
    storage_root = models.CharField(max_length=500, default='/data/trawlr')
    filename_format = models.CharField(
        max_length=20,
        choices=FILENAME_FORMAT_CHOICES,
        default='guid'
    )

    # Download settings
    default_retry_count = models.IntegerField(
        default=3,
        validators=[MinValueValidator(0), MaxValueValidator(10)]
    )

    # Scheduler settings
    download_queue_interval = models.IntegerField(
        default=10,
        choices=SCHEDULER_INTERVAL_CHOICES,
        help_text='How often to check for pending downloads'
    )
    channel_sync_interval = models.IntegerField(
        default=0,
        choices=SCHEDULER_INTERVAL_CHOICES,
        help_text='How often to sync channels for all accounts'
    )
    channel_stats_interval = models.IntegerField(
        default=0,
        choices=SCHEDULER_INTERVAL_CHOICES,
        help_text='How often to refresh channel statistics (member counts)'
    )
    media_counts_interval = models.IntegerField(
        default=0,
        choices=SCHEDULER_INTERVAL_CHOICES,
        help_text='How often to refresh media counts (photos/videos/files)'
    )
    stuck_task_recovery_interval = models.IntegerField(
        default=3600,
        choices=SCHEDULER_INTERVAL_CHOICES,
        help_text='How often to check for and recover stuck tasks (0 = disabled, runs on startup only)'
    )
    availability_check_interval = models.IntegerField(
        default=0,
        choices=SCHEDULER_INTERVAL_CHOICES,
        help_text='How often to check channel availability status (0 = disabled)'
    )
    forum_topics_sync_interval = models.IntegerField(
        default=86400,
        choices=SCHEDULER_INTERVAL_CHOICES,
        help_text='How often to sync forum topics for all forum channels (0 = disabled)'
    )
    member_sync_interval = models.IntegerField(
        default=86400,
        choices=SCHEDULER_INTERVAL_CHOICES,
        help_text='How often to sync group/supergroup member lists (0 = disabled)'
    )
    reaction_scan_interval = models.IntegerField(
        default=0,
        choices=SCHEDULER_INTERVAL_CHOICES,
        help_text='How often to scan for per-user reaction data on messages (0 = disabled). Uses API calls — set conservatively.'
    )
    profile_photo_queue_interval = models.IntegerField(
        default=60,
        choices=SCHEDULER_INTERVAL_CHOICES,
        help_text='How often to dispatch backfill profile-photo downloads for scanned members (0 = disabled). '
                  'Runs at strictly lower priority than file downloads.'
    )

    # Entity Cache Settings
    dialog_cache_limit = models.IntegerField(
        default=500,
        validators=[MinValueValidator(100), MaxValueValidator(5000)],
        help_text='Number of dialogs to fetch when populating the Telegram entity cache (used for availability checks). '
                  'Increase if channels are incorrectly marked as deleted.'
    )

    # Event Processing Settings
    event_processing_enabled = models.BooleanField(
        default=True,
        help_text='Master switch for event processing. If disabled, events will queue but not process.'
    )
    event_processor_batch_size = models.IntegerField(
        default=50,
        validators=[MinValueValidator(1), MaxValueValidator(500)],
        help_text='Number of events to process per worker cycle'
    )
    event_processor_retry_count = models.IntegerField(
        default=5,
        validators=[MinValueValidator(0), MaxValueValidator(10)],
        help_text='Max retries for failed event processing'
    )
    event_processor_retry_backoff_min = models.IntegerField(
        default=10,
        validators=[MinValueValidator(1), MaxValueValidator(300)],
        help_text='Minimum retry backoff in seconds'
    )
    event_processor_retry_backoff_max = models.IntegerField(
        default=300,
        validators=[MinValueValidator(10), MaxValueValidator(3600)],
        help_text='Maximum retry backoff in seconds'
    )

    # Raw Event Storage
    store_raw_events = models.BooleanField(
        default=False,
        help_text='Store raw JSON event data before processing (for debugging/auditing)'
    )
    stream_raw_events = models.BooleanField(
        default=False,
        help_text='Stream raw events to trawlr.events.raw RabbitMQ queue for external processing'
    )

    # Source Onboarding
    run_onboarding_for_new_sources = models.BooleanField(
        default=False,
        help_text='Automatically run onboarding tasks (fetch history & members) for newly detected sources during channel sync'
    )

    # Auto-archive unavailable sources
    auto_archive_unavailable = models.BooleanField(
        default=False,
        help_text='Automatically archive sources that are detected as unavailable (restricted, deleted, private) during availability checks'
    )

    # Source Auto-Discovery
    auto_discover_sources = models.BooleanField(
        default=False,
        help_text='Automatically create sources when the listener receives events from unknown channels/groups'
    )

    # TG Link Resolution
    tglink_resolution = models.BooleanField(
        default=False,
        help_text='Automatically resolve t.me invite links found in messages to catalog destination metadata'
    )

    # Global File Deduplication
    file_deduplication_enabled = models.BooleanField(
        default=True,
        help_text='Skip downloading files that already exist (matched by Telegram file_unique_id). '
                  'Duplicate records will link to the original file instead of downloading again.'
    )

    # Storage Provider
    STORAGE_PROVIDER_CHOICES = [
        ('local', 'Local Filesystem'),
        ('s3', 'S3-Compatible Storage'),
        ('azure', 'Azure Blob Storage'),
    ]
    storage_provider = models.CharField(
        max_length=10,
        choices=STORAGE_PROVIDER_CHOICES,
        default='local',
        help_text='Where to store downloaded media files'
    )

    # S3-Compatible Storage Settings
    s3_endpoint_url = models.CharField(
        max_length=500,
        blank=True,
        default='',
        help_text='S3 endpoint URL (e.g. https://s3.amazonaws.com, or your MinIO/Wasabi URL)'
    )
    s3_access_key_id = models.CharField(
        max_length=200,
        blank=True,
        default='',
        help_text='S3 access key ID'
    )
    s3_secret_access_key = models.CharField(
        max_length=200,
        blank=True,
        default='',
        help_text='S3 secret access key'
    )
    s3_bucket_name = models.CharField(
        max_length=200,
        blank=True,
        default='',
        help_text='S3 bucket name'
    )
    s3_region = models.CharField(
        max_length=50,
        blank=True,
        default='',
        help_text='S3 region (e.g. us-east-1). Leave blank for non-AWS providers.'
    )
    s3_presigned_url_expiry = models.IntegerField(
        default=3600,
        validators=[MinValueValidator(60), MaxValueValidator(86400)],
        help_text='Presigned URL expiry time in seconds (60-86400)'
    )

    # Azure Blob Storage Settings
    azure_connection_string = models.TextField(
        blank=True,
        default='',
        help_text='Azure Blob Storage connection string'
    )
    azure_container_name = models.CharField(
        max_length=200,
        blank=True,
        default='',
        help_text='Azure Blob container name'
    )
    azure_sas_expiry = models.IntegerField(
        default=3600,
        validators=[MinValueValidator(60), MaxValueValidator(86400)],
        help_text='SAS token expiry time in seconds (60-86400)'
    )

    # Track all changes with django-simple-history
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Global Settings'
        verbose_name_plural = 'Global Settings'

    def save(self, *args, **kwargs):
        # Enforce singleton pattern - always use pk=1
        self.pk = 1
        super().save(*args, **kwargs)
        # Invalidate storage backend cache when settings change
        from storage.utils import invalidate_backend_cache
        invalidate_backend_cache()

    def delete(self, *args, **kwargs):
        # Prevent deletion of settings
        pass

    @classmethod
    def get_settings(cls):
        """Get or create the singleton settings instance."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return 'Global Settings'
