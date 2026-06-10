"""
Audit app models for Telegram channels/groups/chats.
"""

import hashlib
from datetime import timedelta
from typing import NamedTuple

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.db.models.functions import Upper
from django.utils import timezone
from simple_history.models import HistoricalRecords

from accounts.models import TelegramAccount


class _IndexedHistoricalRecords(HistoricalRecords):
    """HistoricalRecords subclass that supports adding extra indexes."""

    def __init__(self, extra_indexes=None, **kwargs):
        super().__init__(**kwargs)
        self.extra_indexes = extra_indexes or []

    def get_meta_options(self, model):
        opts = super().get_meta_options(model)
        if self.extra_indexes:
            indexes = list(opts.get('indexes', ()))
            indexes.extend(self.extra_indexes)
            opts['indexes'] = indexes
        return opts

# Signals
from django.db.models.signals import post_save
from django.dispatch import receiver


TAG_COLOUR_CHOICES = [
    ('blue', 'Blue'),
    ('green', 'Green'),
    ('red', 'Red'),
    ('yellow', 'Yellow'),
    ('purple', 'Purple'),
    ('orange', 'Orange'),
    ('pink', 'Pink'),
    ('teal', 'Teal'),
    ('grey', 'Grey'),
]


class Tag(models.Model):
    """Reusable tag for categorising sources and users."""
    name = models.CharField(max_length=100, unique=True)
    colour = models.CharField(max_length=20, choices=TAG_COLOUR_CHOICES, default='blue')
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class TelegramChannelQuerySet(models.QuerySet):
    """Custom QuerySet for TelegramChannel with common filters."""

    def from_active_accounts(self):
        """Return only channels from active accounts."""
        return self.filter(account__is_active=True)

    def active_sources(self):
        """Return only active channels from active accounts."""
        return self.filter(account__is_active=True, active=True)


class TelegramChannelManager(models.Manager):
    """Custom manager for TelegramChannel."""

    def get_queryset(self):
        return TelegramChannelQuerySet(self.model, using=self._db)

    def from_active_accounts(self):
        """Return only channels from active accounts."""
        return self.get_queryset().from_active_accounts()

    def active_sources(self):
        """Return only active channels from active accounts."""
        return self.get_queryset().active_sources()


class TelegramChannel(models.Model):
    """A Telegram channel/group/chat synced from an account."""

    CHANNEL_TYPE_CHOICES = [
        ('channel', 'Channel'),
        ('supergroup', 'Supergroup'),
        ('group', 'Group'),
        ('private', 'Private Chat'),
    ]

    telegram_id = models.BigIntegerField(unique=True, db_index=True)
    account = models.ForeignKey(
        TelegramAccount,
        on_delete=models.CASCADE,
        related_name='channels'
    )
    title = models.CharField(max_length=255)
    username = models.CharField(max_length=255, blank=True, null=True)
    channel_type = models.CharField(max_length=20, choices=CHANNEL_TYPE_CHOICES)
    is_private = models.BooleanField(default=False)
    member_count = models.IntegerField(default=0)

    # Avatar/thumbnail
    avatar = models.ImageField(upload_to='channel_avatars/', blank=True, null=True)

    # User preferences (DEPRECATED - use UserMonitoredSource instead)
    is_favourite = models.BooleanField(default=False, help_text='DEPRECATED: Use UserMonitoredSource for user-based pinning')

    # When the account joined this channel on Telegram
    joined_at = models.DateTimeField(null=True, blank=True, help_text='When the account joined this channel')

    # Telegram verification/warning flags
    is_verified = models.BooleanField(default=False, help_text='Has Telegram verification badge')
    is_scam = models.BooleanField(default=False, help_text='Flagged as scam by Telegram')
    is_fake = models.BooleanField(default=False, help_text='Flagged as fake by Telegram')
    is_restricted = models.BooleanField(default=False, help_text='Has content restrictions')

    # Channel type flags
    is_broadcast = models.BooleanField(default=False, help_text='True broadcast channel (not group)')
    is_megagroup = models.BooleanField(default=False, help_text='Supergroup')
    is_gigagroup = models.BooleanField(default=False, help_text='Broadcast group (large group)')

    # Channel features
    has_signatures = models.BooleanField(default=False, help_text='Shows author signatures on posts')
    has_linked_chat = models.BooleanField(default=False, help_text='Has linked discussion group')
    slowmode_seconds = models.IntegerField(default=0, help_text='Slowmode delay in seconds')
    is_forum = models.BooleanField(default=False, help_text='Forum/topics enabled')
    noforwards = models.BooleanField(default=False, help_text='Forwarding content is prohibited')

    # Join requirements
    join_to_send = models.BooleanField(default=False, help_text='Must join to send messages')
    join_request = models.BooleanField(default=False, help_text='Join requests required')

    # Boost/level
    boost_level = models.IntegerField(null=True, blank=True, help_text='Channel boost level')

    # Account relationship
    has_left = models.BooleanField(default=False, help_text='Account has left this channel')
    active = models.BooleanField(default=True, help_text='Whether Trawlr should process this source (disabled on leave)')

    # Onboarding status
    onboarded = models.BooleanField(default=False, help_text='Whether onboarding tasks have been triggered for this channel')

    # Join provenance (populated when joined via Trawlr UI; blank for auto-discovered channels)
    joined_via_url = models.URLField(
        max_length=2000, blank=True, default='',
        help_text='Original URL the user submitted to Trawlr to join this channel'
    )
    joined_via_invite_hash = models.CharField(
        max_length=255, blank=True, default='',
        help_text='Invite hash extracted from the join URL (empty if joined by username)'
    )

    # Availability status (for detecting deleted/banned channels)
    AVAILABILITY_STATUS_CHOICES = [
        ('active', 'Active'),
        ('unavailable', 'Unavailable'),  # Generic unavailable
        ('deleted', 'Deleted'),  # Channel was deleted
        ('restricted', 'Restricted'),  # Access restricted
        ('private', 'Private'),  # Can no longer access (kicked/left)
        ('unknown', 'Unknown'),  # Check failed with unknown error
    ]
    availability_status = models.CharField(
        max_length=20,
        choices=AVAILABILITY_STATUS_CHOICES,
        default='active',
        help_text='Current availability status of the channel'
    )
    availability_checked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When availability was last checked'
    )
    availability_error = models.TextField(
        blank=True,
        help_text='Error message if channel is unavailable'
    )

    # Multi-account visibility tracking
    # Tracks which accounts have received events from this channel
    # (beyond the primary 'account' owner)
    seen_by_accounts = models.ManyToManyField(
        TelegramAccount,
        related_name='visible_channels',
        blank=True,
        help_text='Accounts that have received events from this channel'
    )

    # Telegram media counts (cached from GetSearchCountersRequest)
    telegram_photo_count = models.IntegerField(default=0, help_text='Total photos in channel')
    telegram_video_count = models.IntegerField(default=0, help_text='Total videos in channel')
    telegram_file_count = models.IntegerField(default=0, help_text='Total files in channel (docs + voice + round + gif)')
    telegram_counts_updated_at = models.DateTimeField(null=True, blank=True, help_text='When media counts were last fetched')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    tags = models.ManyToManyField(Tag, blank=True, related_name='channels')

    # Custom manager with active account filtering
    objects = TelegramChannelManager()

    # Track all changes with django-simple-history
    history = _IndexedHistoricalRecords(
        extra_indexes=[
            models.Index(
                fields=['id', '-history_date', '-history_id'],
                name='hist_tgch_id_date',
            ),
        ],
    )

    class Meta:
        ordering = ['title']
        verbose_name = 'Telegram Channel'
        verbose_name_plural = 'Telegram Channels'

    def __str__(self):
        return self.title

    @property
    def display_name(self):
        """Return display name with username if available."""
        if self.username:
            return f"{self.title} (@{self.username})"
        return self.title

    @property
    def telegram_link(self):
        """Return public Telegram link if available."""
        if self.username:
            return f"https://t.me/{self.username}"
        return None


class ForumTopic(models.Model):
    """
    Tracks forum topics in supergroups with topics enabled.
    Topics are identified by their root message ID within the channel.
    """

    channel = models.ForeignKey(
        TelegramChannel,
        on_delete=models.CASCADE,
        related_name='topics'
    )
    # Topic ID is the message_id of the topic's root message
    topic_id = models.BigIntegerField(db_index=True)
    title = models.CharField(max_length=255)

    # Topic metadata
    icon_color = models.IntegerField(null=True, blank=True, help_text='Topic icon color ID')
    icon_emoji_id = models.BigIntegerField(null=True, blank=True, help_text='Custom emoji ID for topic icon')

    # Topic state flags
    is_closed = models.BooleanField(default=False, help_text='Topic is closed for new messages')
    is_hidden = models.BooleanField(default=False, help_text='Topic is hidden from list')
    is_pinned = models.BooleanField(default=False, help_text='Topic is pinned')
    is_general = models.BooleanField(default=False, help_text='General topic (topic_id=1)')

    # Stats
    message_count = models.IntegerField(default=0, help_text='Archived messages in this topic')

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Track all changes with django-simple-history
    history = HistoricalRecords()

    class Meta:
        unique_together = ['channel', 'topic_id']
        ordering = ['title']
        verbose_name = 'Forum Topic'
        verbose_name_plural = 'Forum Topics'
        indexes = [
            models.Index(fields=['channel', 'topic_id']),
        ]

    def __str__(self):
        return f"{self.title} (in {self.channel.title})"


class ChannelConfig(models.Model):
    """Configuration for a channel's download behavior."""

    PRIORITY_CHOICES = [(i, str(i)) for i in range(1, 11)]  # 1=lowest, 10=highest

    DEDUP_CHOICES = [
        ('none', 'No Deduplication'),
        ('sha256', 'SHA256 Hash (Link to Original)'),
    ]

    THUMBNAIL_SIZE_CHOICES = [
        ('s', 'Small (~100px)'),
        ('m', 'Medium (~320px)'),
        ('x', 'Large (~800px)'),
        ('y', 'Extra Large (~1280px)'),
    ]

    channel = models.OneToOneField(
        TelegramChannel,
        on_delete=models.CASCADE,
        related_name='config'
    )

    # Download settings
    auto_download_enabled = models.BooleanField(
        default=False,
        help_text='Automatically download new media from this source'
    )
    download_photos = models.BooleanField(default=False)
    download_videos = models.BooleanField(default=False)
    download_files = models.BooleanField(
        default=False,
        help_text='Documents, audio, voice messages, stickers, etc.'
    )

    # File type priority order (first = highest priority)
    file_type_priority = models.JSONField(
        default=list,
        blank=True,
        help_text='Order in which file types are downloaded (first = highest priority)'
    )

    # Priority (higher = processed first)
    priority = models.IntegerField(
        default=5,
        choices=PRIORITY_CHOICES,
        help_text='Higher priority sources are processed first (1-10)'
    )

    # Deduplication
    deduplication_mode = models.CharField(
        max_length=20,
        choices=DEDUP_CHOICES,
        default='none',
        help_text='How to handle duplicate files'
    )

    # Archive settings
    archive_enabled = models.BooleanField(
        default=False,
        help_text='Archive text messages from this source'
    )

    # Thumbnail settings
    download_thumbnails = models.BooleanField(
        default=False,
        help_text='Always download thumbnails for media posts (enables browsing without full downloads)'
    )
    thumbnail_size = models.CharField(
        max_length=1,
        choices=THUMBNAIL_SIZE_CHOICES,
        default='m',
        help_text='Size of downloaded thumbnails (smaller = less storage, larger = better preview)'
    )

    # Queue state
    is_paused = models.BooleanField(
        default=False,
        help_text='Pause all downloads from this source'
    )

    # Listener bypass
    bypass_listener = models.BooleanField(
        default=False,
        help_text='Skip real-time message tracking for this source'
    )

    # Progress tracking
    last_downloaded_message_id = models.BigIntegerField(default=0)
    total_messages = models.IntegerField(default=0)
    downloaded_messages = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Track all changes with django-simple-history
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Channel Config'
        verbose_name_plural = 'Channel Configs'

    def __str__(self):
        return f"Config for {self.channel.title}"

    @property
    def download_progress_percent(self):
        """Calculate download progress as percentage."""
        if self.total_messages == 0:
            return 0
        return int((self.downloaded_messages / self.total_messages) * 100)

    def get_file_type_priority(self, file_type: str) -> int:
        """
        Get the priority boost for a file type based on configured order.
        Returns a value from 0-2 to add to base priority (higher = download first).
        """
        default_order = ['photo', 'video', 'file']
        order = self.file_type_priority if self.file_type_priority else default_order

        try:
            # First in list = highest boost (2), last = 0
            index = order.index(file_type)
            return max(0, 2 - index)
        except ValueError:
            return 0

    def get_effective_file_type_priority(self) -> list:
        """Return the file type priority list, using defaults if empty."""
        if self.file_type_priority:
            return self.file_type_priority
        return ['photo', 'video', 'file']

    def sync_pending_task_priorities(self):
        """
        Update priorities of all pending download tasks for this channel
        to match the current config priority settings.
        """
        # Nested import to avoid circular dependency with downloads.models
        from downloads.models import DownloadTask

        pending_tasks = DownloadTask.objects.filter(
            channel=self.channel,
            status__in=['pending', 'paused']
        )

        for task in pending_tasks:
            new_priority = self.priority + self.get_file_type_priority(task.file_type)
            if task.priority != new_priority:
                task.priority = new_priority
                task.save(update_fields=['priority'])


class UserMonitoredSource(models.Model):
    """
    Track which sources a user is monitoring/has pinned.
    Enables per-user channel monitoring with an aggregate feed view.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='monitored_sources'
    )
    channel = models.ForeignKey(
        TelegramChannel,
        on_delete=models.CASCADE,
        related_name='monitored_by'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # Track all changes with django-simple-history
    history = HistoricalRecords()

    class Meta:
        unique_together = ['user', 'channel']
        ordering = ['-created_at']
        verbose_name = 'Monitored Source'
        verbose_name_plural = 'Monitored Sources'

    def __str__(self):
        return f"{self.user.username} monitors {self.channel.title}"


class TelegramUser(models.Model):
    """
    Identified Telegram user tracked across all monitored sources.
    Uses django-simple-history to track profile changes over time.
    """
    telegram_id = models.BigIntegerField(unique=True, db_index=True)

    # Current profile info (updated on each sighting)
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    username = models.CharField(max_length=255, blank=True, db_index=True)
    phone = models.CharField(max_length=50, blank=True)

    # Core flags
    is_bot = models.BooleanField(default=False)
    is_verified = models.BooleanField(default=False)
    is_premium = models.BooleanField(default=False)
    is_scam = models.BooleanField(default=False)
    is_fake = models.BooleanField(default=False)
    is_restricted = models.BooleanField(default=False)

    # Additional status flags
    is_deleted = models.BooleanField(default=False, help_text='User has deleted their account')
    is_support = models.BooleanField(default=False, help_text='Official Telegram support account')

    # Contact relationship (relative to the monitoring account)
    is_contact = models.BooleanField(default=False, help_text='Is in account contacts')
    is_mutual_contact = models.BooleanField(default=False, help_text='Mutual contact status')
    is_close_friend = models.BooleanField(default=False, help_text='In close friends list')

    # Additional profile info
    lang_code = models.CharField(max_length=10, blank=True, help_text='User language preference')
    stories_hidden = models.BooleanField(default=False, help_text='Stories hidden from account')
    stories_unavailable = models.BooleanField(default=False, help_text='No stories available')
    emoji_status = models.JSONField(null=True, blank=True, help_text='Custom emoji status')

    # API access (useful for further API calls)
    access_hash = models.BigIntegerField(null=True, blank=True, help_text='Telegram access hash for API calls')

    # Profile photo
    photo_id = models.BigIntegerField(null=True, blank=True)
    photo_path = models.CharField(max_length=500, blank=True)
    profile_photo_base64 = models.TextField(
        blank=True,
        help_text='Base64 encoded profile photo (not stored as file for safety)'
    )
    profile_photo_updated_at = models.DateTimeField(null=True, blank=True)
    # One-time claim set by the profile-photo backfill dispatcher when it enqueues a
    # download for this user. The dispatcher never re-picks a user once this is set.
    profile_photo_attempted_at = models.DateTimeField(null=True, blank=True)

    # Full profile data (from GetFullUserRequest) - OSINT fields
    bio = models.TextField(blank=True, help_text='User bio/about text')
    birthday = models.CharField(max_length=20, blank=True, help_text='Birthday if set')
    private_forward_name = models.CharField(max_length=255, blank=True, help_text='Name shown when forwarding is restricted')
    personal_channel_id = models.BigIntegerField(null=True, blank=True, help_text='Personal channel ID')
    common_chats_count = models.IntegerField(default=0, help_text='Number of common chats with scanning account')

    # Communication availability
    phone_calls_available = models.BooleanField(default=False, help_text='Can receive phone calls')
    video_calls_available = models.BooleanField(default=False, help_text='Can receive video calls')
    voice_messages_forbidden = models.BooleanField(default=False, help_text='Blocks voice messages')
    contact_require_premium = models.BooleanField(default=False, help_text='Requires premium to contact')

    # Relationship with scanning account
    is_blocked = models.BooleanField(default=False, help_text='User is blocking scanning account')

    # Business profile (if applicable)
    business_intro = models.TextField(blank=True, help_text='Business introduction text')
    business_location = models.CharField(max_length=500, blank=True, help_text='Business location')
    business_work_hours = models.JSONField(null=True, blank=True, help_text='Business work hours')

    # Activity indicators
    has_pinned_stories = models.BooleanField(default=False, help_text='Has pinned stories')
    has_scheduled_messages = models.BooleanField(default=False, help_text='Uses scheduled messages')
    pinned_message_id = models.BigIntegerField(null=True, blank=True, help_text='Pinned message ID in DM')

    # Full profile fetch tracking
    full_profile_fetched_at = models.DateTimeField(null=True, blank=True, help_text='When full profile was last fetched')

    # Tracking
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    message_count = models.IntegerField(default=0)

    # Flagging for review (before reporting)
    is_flagged = models.BooleanField(default=False, help_text='Flagged for review/reporting')
    flagged_reason = models.CharField(max_length=50, blank=True, choices=[
        ('spam', 'Spam'),
        ('violence', 'Violence'),
        ('pornography', 'Adult Content'),
        ('child_abuse', 'Child Abuse'),
        ('copyright', 'Copyright Violation'),
        ('fake', 'Fake Account / Scam'),
        ('illegal_drugs', 'Illegal Drugs'),
        ('personal_details', 'Personal Data Exposure'),
        ('other', 'Other'),
    ])
    flagged_notes = models.TextField(blank=True, help_text='Notes about why user was flagged')
    flagged_at = models.DateTimeField(null=True, blank=True)

    # Reporting status
    reported_to_telegram = models.BooleanField(default=False)
    reported_at = models.DateTimeField(null=True, blank=True)

    # Custom fields for investigation
    notes = models.TextField(blank=True)
    tags = models.ManyToManyField(Tag, blank=True, related_name='users')

    # Track all changes with django-simple-history
    # Exclude volatile fields that change on every message to prevent history bloat
    history = HistoricalRecords(
        excluded_fields=['last_seen', 'message_count', 'profile_photo_base64', 'profile_photo_attempted_at']
    )

    class Meta:
        ordering = ['-last_seen']
        verbose_name = 'Telegram User'
        verbose_name_plural = 'Telegram Users'
        indexes = [
            models.Index(fields=['username']),
            models.Index(fields=['first_name', 'last_name']),
            models.Index(fields=['last_seen']),
        ]

    def __str__(self):
        if self.username:
            return f"@{self.username}"
        if self.first_name or self.last_name:
            return f"{self.first_name} {self.last_name}".strip()
        return f"User {self.telegram_id}"

    @property
    def display_name(self):
        """Return best available display name."""
        name = f"{self.first_name} {self.last_name}".strip()
        if name and self.username:
            return f"{name} (@{self.username})"
        if self.username:
            return f"@{self.username}"
        if name:
            return name
        return f"User {self.telegram_id}"

    @property
    def telegram_link(self):
        """Return link to user profile if username available."""
        if self.username:
            return f"https://t.me/{self.username}"
        return None

    def update_from_telethon(self, user_obj):
        """
        Update this record from a Telethon User object.
        Returns True if any tracked fields changed.
        """
        changed = False

        new_values = {
            'first_name': getattr(user_obj, 'first_name', '') or '',
            'last_name': getattr(user_obj, 'last_name', '') or '',
            'username': getattr(user_obj, 'username', '') or '',
            'phone': getattr(user_obj, 'phone', '') or '',
            'is_bot': getattr(user_obj, 'bot', False) or False,
            'is_verified': getattr(user_obj, 'verified', False) or False,
            'is_premium': getattr(user_obj, 'premium', False) or False,
            'is_scam': getattr(user_obj, 'scam', False) or False,
            'is_fake': getattr(user_obj, 'fake', False) or False,
            'is_restricted': getattr(user_obj, 'restricted', False) or False,
            # New fields
            'is_deleted': getattr(user_obj, 'deleted', False) or False,
            'is_support': getattr(user_obj, 'support', False) or False,
            'is_contact': getattr(user_obj, 'contact', False) or False,
            'is_mutual_contact': getattr(user_obj, 'mutual_contact', False) or False,
            'is_close_friend': getattr(user_obj, 'close_friend', False) or False,
            'lang_code': getattr(user_obj, 'lang_code', '') or '',
            'stories_hidden': getattr(user_obj, 'stories_hidden', False) or False,
            'stories_unavailable': getattr(user_obj, 'stories_unavailable', False) or False,
        }

        for field, new_value in new_values.items():
            if getattr(self, field) != new_value:
                setattr(self, field, new_value)
                changed = True

        # Update access_hash if available
        new_access_hash = getattr(user_obj, 'access_hash', None)
        if new_access_hash and self.access_hash != new_access_hash:
            self.access_hash = new_access_hash
            changed = True

        # Update emoji_status if available
        emoji_status = getattr(user_obj, 'emoji_status', None)
        if emoji_status:
            new_emoji = {'emoji_id': getattr(emoji_status, 'document_id', None)}
            if self.emoji_status != new_emoji:
                self.emoji_status = new_emoji
                changed = True

        # Update photo if changed
        if hasattr(user_obj, 'photo') and user_obj.photo:
            new_photo_id = getattr(user_obj.photo, 'photo_id', None)
            if new_photo_id and self.photo_id != new_photo_id:
                self.photo_id = new_photo_id
                changed = True

        self.last_seen = timezone.now()
        return changed


class TelegramUserAlias(models.Model):
    """
    Append-only record of distinct (first_name, last_name, username) combinations
    observed for a TelegramUser. Captures name/handle history when Trawlr doesn't detect the original Telegram change event. 
    A new alias row appears
    the next time we ingest a message under the new name.
    """
    user = models.ForeignKey(
        TelegramUser,
        on_delete=models.CASCADE,
        related_name='aliases',
    )
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    username = models.CharField(max_length=255, blank=True, db_index=True)

    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    observation_count = models.PositiveIntegerField(default=1)

    first_seen_channel = models.ForeignKey(
        TelegramChannel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
        help_text='Channel where this alias was first observed',
    )
    first_seen_message_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text='Telegram message_id where this alias was first observed',
    )

    class Meta:
        ordering = ['-last_seen']
        verbose_name = 'Telegram User Alias'
        verbose_name_plural = 'Telegram User Aliases'
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'first_name', 'last_name', 'username'],
                name='unique_user_alias_triple',
            ),
        ]
        indexes = [
            models.Index(fields=['user', '-last_seen']),
            models.Index(fields=['username']),
        ]

    def __str__(self):
        parts = [p for p in (self.first_name, self.last_name) if p]
        name = ' '.join(parts)
        if self.username:
            return f"{name} (@{self.username})" if name else f"@{self.username}"
        return name or f"alias#{self.pk}"


class UserGroupMembership(models.Model):
    """
    Tracks which users are members of which groups/channels.
    Updated when users are seen posting or through member list scans.
    """
    user = models.ForeignKey(
        TelegramUser,
        on_delete=models.CASCADE,
        related_name='memberships'
    )
    channel = models.ForeignKey(
        TelegramChannel,
        on_delete=models.CASCADE,
        related_name='user_memberships'
    )

    # Timestamps
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    last_message_date = models.DateTimeField(null=True, blank=True)
    left_at = models.DateTimeField(null=True, blank=True, help_text='When the user left the group')

    # Membership status
    active = models.BooleanField(default=True, help_text='False if user explicitly left/was kicked')

    # Role in group (if known)
    is_admin = models.BooleanField(default=False)
    is_creator = models.BooleanField(default=False)
    admin_title = models.CharField(max_length=255, blank=True)

    # Stats
    message_count = models.IntegerField(default=0)

    # Track changes
    history = HistoricalRecords()

    class Meta:
        unique_together = ['user', 'channel']
        ordering = ['-last_seen']
        verbose_name = 'User Membership'
        verbose_name_plural = 'User Memberships'
        indexes = [
            models.Index(fields=['user', 'active'], name='ugm_user_active_idx'),
            models.Index(fields=['active', 'last_seen'], name='ugm_active_lastseen_idx'),
            models.Index(
                fields=['user'],
                name='ugm_active_user_partial_idx',
                condition=models.Q(active=True),
            ),
        ]

    def __str__(self):
        return f"{self.user} in {self.channel}"


class ExclusionRule(models.Model):
    """
    Tracks users to exclude from listener message processing.
    Supports both global exclusions (all sources) and source-specific exclusions.
    """
    telegram_user = models.ForeignKey(
        TelegramUser,
        on_delete=models.CASCADE,
        related_name='exclusion_rules'
    )
    # Null = global exclusion; Set = source-specific exclusion
    source = models.ForeignKey(
        TelegramChannel,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='exclusion_rules'
    )
    is_global = models.BooleanField(default=False, help_text='If true, applies to all sources')
    is_active = models.BooleanField(default=True, help_text='Toggle exclusion on/off')
    reason = models.TextField(blank=True, default='', help_text='Optional reason for exclusion')
    trigger_count = models.PositiveIntegerField(default=0, help_text='Times this exclusion blocked a message')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Track all changes with django-simple-history
    history = HistoricalRecords()

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Exclusion Rule'
        verbose_name_plural = 'Exclusion Rules'
        constraints = [
            models.UniqueConstraint(
                fields=['telegram_user'],
                condition=models.Q(is_global=True),
                name='unique_global_exclusion_per_user'
            ),
            models.UniqueConstraint(
                fields=['telegram_user', 'source'],
                condition=models.Q(is_global=False),
                name='unique_source_exclusion_per_user'
            ),
        ]
        indexes = [
            # Optimized index for listener exclusion lookups
            # Query pattern: filter(telegram_user=X, is_active=True).filter(Q(is_global=True) | Q(source_id=Y))
            models.Index(
                fields=['telegram_user', 'is_active', 'is_global'],
                name='exclusion_listener_lookup_idx'
            ),
        ]

    def __str__(self):
        if self.is_global:
            return f"Global exclusion: {self.telegram_user}"
        return f"Exclusion: {self.telegram_user} from {self.source}"


class TelegramReport(models.Model):
    """Track reports submitted to Telegram."""

    REPORT_TYPE_CHOICES = [
        ('channel', 'Channel'),
        ('user', 'User'),
        ('message', 'Message'),
    ]

    REASON_CHOICES = [
        ('spam', 'Spam'),
        ('violence', 'Violence'),
        ('pornography', 'Adult Content'),
        ('child_abuse', 'Child Abuse'),
        ('copyright', 'Copyright Violation'),
        ('fake', 'Fake Account / Scam'),
        ('illegal_drugs', 'Illegal Drugs'),
        ('personal_details', 'Personal Data Exposure'),
        ('other', 'Other'),
    ]

    # What was reported
    report_type = models.CharField(max_length=20, choices=REPORT_TYPE_CHOICES)
    reason = models.CharField(max_length=30, choices=REASON_CHOICES)
    message = models.TextField(blank=True, help_text='Additional details provided with report')

    # Links to reported entities (nullable - only one will be set)
    channel = models.ForeignKey(
        'TelegramChannel',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reports'
    )
    user = models.ForeignKey(
        'TelegramUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reports'
    )
    archived_message = models.ForeignKey(
        'downloads.ArchivedMessage',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reports'
    )

    # Store raw IDs in case entities are deleted
    reported_telegram_id = models.BigIntegerField(help_text='Telegram ID of reported entity')
    reported_message_id = models.IntegerField(null=True, blank=True, help_text='Message ID if reporting a message')
    reported_name = models.CharField(max_length=255, blank=True, help_text='Name/title at time of report')

    # Which account submitted the report
    account = models.ForeignKey(
        'accounts.TelegramAccount',
        on_delete=models.SET_NULL,
        null=True,
        related_name='submitted_reports'
    )

    # Result
    success = models.BooleanField(default=False)
    error_message = models.TextField(blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    # Track all changes with django-simple-history
    history = HistoricalRecords()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_report_type_display()} report: {self.reported_name} ({self.get_reason_display()})"


class MessageEntity(models.Model):
    """
    Text entities extracted from messages (URLs, mentions, hashtags, etc.)
    Enables searching for links, tracking shared URLs, and analyzing mentions.
    """

    ENTITY_TYPE_CHOICES = [
        ('url', 'URL'),
        ('text_url', 'Text URL'),  # URL with custom display text
        ('domain', 'Domain'),  # Extracted domain from URL (custom parser)
        ('mention', '@Mention'),
        ('mention_name', 'User Mention'),  # Mention with user ID
        ('hashtag', 'Hashtag'),
        ('cashtag', 'Cashtag'),
        ('bot_command', 'Bot Command'),
        ('email', 'Email'),
        ('phone', 'Phone Number'),
        ('bold', 'Bold'),
        ('italic', 'Italic'),
        ('underline', 'Underline'),
        ('strikethrough', 'Strikethrough'),
        ('code', 'Code'),
        ('pre', 'Pre-formatted'),
        ('spoiler', 'Spoiler'),
        ('blockquote', 'Blockquote'),
        ('custom_emoji', 'Custom Emoji'),
    ]

    message = models.ForeignKey(
        'downloads.ArchivedMessage',
        on_delete=models.CASCADE,
        related_name='entities'
    )
    channel = models.ForeignKey(
        'audit.TelegramChannel',
        on_delete=models.CASCADE,
        related_name='message_entities',
        null=True,
        blank=True,
    )
    # Links to the deduplicated entity record. All identity fields
    # (entity_type, text, url, user_id, custom_emoji_id, language) live on
    # GlobalEntity — this row just captures where a given entity appears in
    # a given message.
    entity = models.ForeignKey(
        'audit.GlobalEntity',
        on_delete=models.PROTECT,
        related_name='occurrences',
    )
    offset = models.IntegerField(help_text='Start position in text')
    length = models.IntegerField(help_text='Length of entity')

    # Track all changes with django-simple-history
    history = HistoricalRecords()

    class Meta:
        ordering = ['offset']
        verbose_name = 'Message Entity'
        verbose_name_plural = 'Message Entities'
        indexes = [
            # Access paths: "entities for this message" and "entities for this channel"
            # — identity-field indexes moved to GlobalEntity in Phase 1.
            models.Index(fields=['message']),
            models.Index(fields=['channel']),
            models.Index(fields=['entity']),
        ]

    def __str__(self):
        if self.entity_id:
            return f"{self.entity.entity_type}: {(self.entity.text or self.entity.url or '')[:50]}"
        return f"<orphan entity {self.pk}>"


# Process-local LRU-ish cache used by GlobalEntity.bulk_get_or_create.
# Worker-scoped, cleared on restart — entities are immutable so stale lookups are impossible.
_GLOBAL_ENTITY_ID_CACHE: dict[str, int] = {}
_GLOBAL_ENTITY_CACHE_MAX = 10_000


class BulkResolveResult(NamedTuple):
    """Result of :meth:`GlobalEntity.bulk_resolve`."""
    hash_to_id: dict
    newly_created_hashes: set


class GlobalEntity(models.Model):
    """
    Deduplicated entity-identity record. One row per unique tuple of
    (entity_type, text, url, user_id, custom_emoji_id, language).

    MessageEntity rows point at GlobalEntity via the ``entity`` FK; this lets
    the same URL/hashtag/mention be stored once and referenced by every
    message occurrence. The per-occurrence position (offset, length, message,
    channel) stays on MessageEntity.

    Dedup key is ``content_hash`` — a BLAKE2b-128 hex digest of the field
    tuple (see compute_hash). A unique index on content_hash is the only
    thing enforcing uniqueness; the individual fields are not independently
    unique. This lets long URLs / text live here without blowing past
    postgres btree key-size limits.
    """

    entity_type = models.CharField(max_length=20, choices=MessageEntity.ENTITY_TYPE_CHOICES)
    text = models.TextField(blank=True)
    url = models.URLField(max_length=2000, blank=True)
    user_id = models.BigIntegerField(null=True, blank=True)
    custom_emoji_id = models.BigIntegerField(null=True, blank=True)
    language = models.CharField(max_length=50, blank=True)

    content_hash = models.CharField(max_length=32, unique=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Global Entity'
        verbose_name_plural = 'Global Entities'
        indexes = [
            models.Index(fields=['entity_type']),
            models.Index(fields=['user_id']),
            # Trigram GIN on UPPER(...) — Django's icontains/istartswith/
            # iendswith compile to UPPER(col) LIKE UPPER(...), so the index
            # expression must be UPPER(col) to be usable at all.
            GinIndex(
                OpClass(Upper('url'), name='gin_trgm_ops'),
                name='globalentity_url_upper_trgm',
            ),
            GinIndex(
                OpClass(Upper('text'), name='gin_trgm_ops'),
                name='globalentity_text_upper_trgm',
            ),
            # Btree on UPPER(text) for iexact (= UPPER(...)). Partial: only
            # the short, exact-searchable entity types — text on formatting
            # entities (bold/code/blockquote) can exceed the btree key-size
            # limit this model deliberately avoids (see docstring).
            models.Index(
                Upper('text'),
                name='globalentity_text_upper',
                condition=models.Q(entity_type__in=[
                    'hashtag', 'mention', 'email', 'phone', 'domain',
                ]),
            ),
        ]

    def __str__(self):
        return f"{self.entity_type}: {(self.text or self.url or '')[:50]}"

    @classmethod
    def compute_hash(cls, *, entity_type, text, url, user_id, custom_emoji_id, language, **_):
        """
        Compute the dedup hash for an entity tuple. Accepts the same kwargs
        the entities_data dicts carry (extra keys like offset/length are
        accepted via **_ so callers can pass the dict through verbatim).
        Canonicalization: empty string for missing text fields, empty string
        for missing IDs. No case/whitespace normalization — EXACT match only.
        """
        parts = (
            entity_type or '',
            text or '',
            url or '',
            str(user_id) if user_id is not None else '',
            str(custom_emoji_id) if custom_emoji_id is not None else '',
            language or '',
        )
        return hashlib.blake2b('\x00'.join(parts).encode('utf-8'), digest_size=16).hexdigest()

    @classmethod
    def bulk_get_or_create(cls, entity_dicts: list) -> dict:
        """
        Resolve a batch of entity dicts to ``{content_hash: GlobalEntity.id}``.

        Thin wrapper around :meth:`bulk_resolve` that drops the
        newly-created-hashes set. Existing callers that don't care which rows
        were brand new keep working unchanged.
        """
        return cls.bulk_resolve(entity_dicts).hash_to_id

    @classmethod
    def bulk_resolve(cls, entity_dicts: list):
        """
        Like :meth:`bulk_get_or_create` but also returns the set of content
        hashes that were inserted by this call (i.e. brand-new entities,
        first sighting in the system). Used by the notification matcher to
        fire `new_entity`-mode rules.

        Returns a :class:`BulkResolveResult` (``hash_to_id`` dict +
        ``newly_created_hashes`` set).

        Query budget regardless of input size:
        - 0 queries if every entity is already in the process-local cache
        - 1 query if all cache-missed entities already exist in the DB
        - 3 queries if some cache-missed entities are brand new
        """
        if not entity_dicts:
            return BulkResolveResult(hash_to_id={}, newly_created_hashes=set())

        wanted = {cls.compute_hash(**d): d for d in entity_dicts}

        resolved = {h: _GLOBAL_ENTITY_ID_CACHE[h] for h in wanted if h in _GLOBAL_ENTITY_ID_CACHE}
        unresolved = wanted.keys() - resolved.keys()
        if not unresolved:
            return BulkResolveResult(hash_to_id=resolved, newly_created_hashes=set())

        for h, pk in cls.objects.filter(content_hash__in=unresolved).values_list('content_hash', 'id'):
            resolved[h] = pk
            _GLOBAL_ENTITY_ID_CACHE[h] = pk

        newly_created: set = set()
        still_missing = unresolved - resolved.keys()
        if still_missing:
            to_create = [
                cls(
                    content_hash=h,
                    entity_type=wanted[h]['entity_type'],
                    text=wanted[h].get('text', '') or '',
                    url=wanted[h].get('url', '') or '',
                    user_id=wanted[h].get('user_id'),
                    custom_emoji_id=wanted[h].get('custom_emoji_id'),
                    language=wanted[h].get('language', '') or '',
                )
                for h in still_missing
            ]
            cls.objects.bulk_create(to_create, ignore_conflicts=True)
            # `still_missing` captures rows that weren't in the cache *and*
            # weren't in the DB at SELECT time — i.e. as far as this worker
            # can tell, brand-new entities. A concurrent worker inserting the
            # same row at the same instant could cause both workers to count
            # the row as "new"; that's a rare race and acceptable for
            # notification semantics (cooldown_seconds covers the duplicate).
            newly_created = set(still_missing)

            # Refetch — bulk_create(ignore_conflicts=True) doesn't populate pks
            # on Postgres.
            for h, pk in cls.objects.filter(content_hash__in=still_missing).values_list('content_hash', 'id'):
                resolved[h] = pk
                _GLOBAL_ENTITY_ID_CACHE[h] = pk

        if len(_GLOBAL_ENTITY_ID_CACHE) > _GLOBAL_ENTITY_CACHE_MAX:
            for k in list(_GLOBAL_ENTITY_ID_CACHE.keys())[: _GLOBAL_ENTITY_CACHE_MAX // 2]:
                del _GLOBAL_ENTITY_ID_CACHE[k]

        return BulkResolveResult(hash_to_id=resolved, newly_created_hashes=newly_created)


class ForwardSource(models.Model):
    """
    Track the origin of forwarded messages.
    Enables tracking content propagation and identifying original sources.
    """

    SOURCE_TYPE_CHOICES = [
        ('channel', 'Channel'),
        ('user', 'User'),
        ('hidden', 'Hidden Sender'),
    ]

    message = models.OneToOneField(
        'downloads.ArchivedMessage',
        on_delete=models.CASCADE,
        related_name='forward_source'
    )

    # Original source identification
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPE_CHOICES)
    source_telegram_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    source_title = models.CharField(max_length=255, blank=True, help_text='Channel title or user name')
    source_username = models.CharField(max_length=255, blank=True, db_index=True)

    # Original message reference
    original_message_id = models.BigIntegerField(null=True, blank=True, help_text='Original message ID in source')
    original_date = models.DateTimeField(null=True, blank=True, help_text='Original post date')
    original_author = models.CharField(max_length=255, blank=True, help_text='Post author signature')

    # From name (for hidden forwards)
    from_name = models.CharField(max_length=255, blank=True, help_text='Name shown for hidden forward')

    # Source channel metadata (if available)
    source_is_verified = models.BooleanField(default=False)
    source_is_scam = models.BooleanField(default=False)
    source_is_fake = models.BooleanField(default=False)
    source_is_broadcast = models.BooleanField(default=False)

    # Track all changes with django-simple-history
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Forward Source'
        verbose_name_plural = 'Forward Sources'
        indexes = [
            models.Index(fields=['source_telegram_id']),
            models.Index(fields=['source_username']),
            models.Index(fields=['message']),
        ]

    def __str__(self):
        if self.source_title:
            return f"Forwarded from: {self.source_title}"
        if self.from_name:
            return f"Forwarded from: {self.from_name}"
        return f"Forwarded from: {self.source_type} {self.source_telegram_id}"


class ActivityLog(models.Model):
    """
    Real-time activity log for tracking system operations.
    Used to provide visibility into what's happening across containers.
    """

    ACTIVITY_TYPE_CHOICES = [
        # Listener activities
        ('message_processed', 'Message Processed'),
        ('user_tracked', 'User Tracked'),
        ('photo_downloaded', 'Profile Photo Downloaded'),

        # Download activities
        ('download_queued', 'Download Queued'),
        ('download_started', 'Download Started'),
        ('download_completed', 'Download Completed'),
        ('download_failed', 'Download Failed'),

        # History scan activities
        ('history_scan_started', 'History Scan Started'),
        ('history_scan_completed', 'History Scan Completed'),
        ('history_scan_cancelled', 'History Scan Cancelled'),

        # Member scan activities
        ('member_scan_started', 'Member Scan Started'),
        ('member_scan_completed', 'Member Scan Completed'),
        ('member_scan_cancelled', 'Member Scan Cancelled'),

        # Profile fetch activities
        ('profile_fetch_started', 'Profile Fetch Started'),
        ('profile_fetched', 'Profile Fetched'),
        ('profile_fetch_completed', 'Profile Fetch Completed'),

        # Availability check activities
        ('availability_check_started', 'Availability Check Started'),
        ('availability_check_completed', 'Availability Check Completed'),
        ('source_auto_archived', 'Source Auto-Archived'),

        # Channel sync activities
        ('channel_sync_started', 'Channel Sync Started'),
        ('channel_sync_completed', 'Channel Sync Completed'),
        ('channel_sync_cancelled', 'Channel Sync Cancelled'),

        # Stats refresh activities
        ('refresh_stats_started', 'Refresh Stats Started'),
        ('refresh_stats_completed', 'Refresh Stats Completed'),
        ('media_counts_updated', 'Media Counts Updated'),

        # Media management activities
        ('user_media_delete_started', 'User Media Delete Started'),
        ('user_media_delete_completed', 'User Media Delete Completed'),

        # TG Link activities
        ('tglink_resolved', 'Invite Link Resolved'),
        ('tglink_failed', 'Invite Link Resolution Failed'),

        # Reaction scan activities
        ('reaction_scan_completed', 'Reaction Scan Completed'),

        # System activities
        ('flood_wait', 'Flood Wait'),
        ('error', 'Error'),

        # Notification activities
        ('entity_match', 'Entity Match (Watchlist)'),
    ]

    SOURCE_CHOICES = [
        ('listener', 'Listener Service'),
        ('worker', 'Download Worker'),
        ('worker_telegram', 'Telegram Worker'),
        ('web', 'Web'),
        ('notifications', 'Notifications'),
    ]

    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    activity_type = models.CharField(max_length=50, choices=ACTIVITY_TYPE_CHOICES, db_index=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='listener')
    description = models.CharField(max_length=500)

    # Optional references
    channel = models.ForeignKey(
        TelegramChannel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='activity_logs'
    )
    telegram_user = models.ForeignKey(
        'TelegramUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='activity_logs'
    )

    # Extra details as JSON
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['-timestamp', 'activity_type']),
            models.Index(fields=['source', '-timestamp']),
        ]

    def __str__(self):
        return f"[{self.timestamp:%H:%M:%S}] {self.get_activity_type_display()}: {self.description}"

    @classmethod
    def log(cls, activity_type, description, source='listener', channel=None, telegram_user=None, **details):
        """
        Convenience method to create activity log entries.

        Usage:
            ActivityLog.log('message_archived', 'Archived message in Channel X', channel=channel)
        """
        return cls.objects.create(
            activity_type=activity_type,
            description=description,
            source=source,
            channel=channel,
            telegram_user=telegram_user,
            details=details,
        )

    @classmethod
    def cleanup_old(cls, hours=24):
        """Remove activity logs older than specified hours."""
        cutoff = timezone.now() - timedelta(hours=hours)
        deleted, _ = cls.objects.filter(timestamp__lt=cutoff).delete()
        return deleted


class UserNote(models.Model):
    """
    User-created notes that can be attached to Sources (TelegramChannel) or Users (TelegramUser).
    Uses GenericForeignKey to support multiple entity types.
    """
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey('content_type', 'object_id')

    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='notes'
    )

    # Track all changes with django-simple-history
    history = HistoricalRecords()

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['content_type', 'object_id']),
        ]

    def __str__(self):
        return f"Note on {self.content_type.model} #{self.object_id} by {self.created_by}"

    @property
    def is_edited(self):
        """Check if the note has been edited after creation."""
        if self.updated_at and self.created_at:
            # Allow 1 second tolerance for auto_now vs auto_now_add
            return (self.updated_at - self.created_at).total_seconds() > 1
        return False

    @classmethod
    def get_for_object(cls, obj):
        """Get all notes for a given object."""
        content_type = ContentType.objects.get_for_model(obj)
        return cls.objects.filter(content_type=content_type, object_id=obj.pk)

    @classmethod
    def add_note(cls, obj, text, user):
        """Add a note to an object."""
        content_type = ContentType.objects.get_for_model(obj)
        return cls.objects.create(
            content_type=content_type,
            object_id=obj.pk,
            text=text,
            created_by=user
        )


class TelegramLink(models.Model):
    """Stores resolved t.me links found in messages."""

    LINK_TYPES = [
        ('channel', 'Channel'),
        ('user', 'User'),
        ('bot', 'Bot'),
        ('invite', 'Invite'),
        ('unknown', 'Unknown'),
    ]
    STATUSES = [
        ('pending', 'Pending'),
        ('resolved', 'Resolved'),
        ('failed', 'Failed'),
    ]

    url = models.CharField(max_length=500, unique=True, db_index=True)
    identifier = models.CharField(max_length=255, help_text="Username or invite hash extracted from URL")
    link_type = models.CharField(max_length=20, choices=LINK_TYPES, default='unknown')
    status = models.CharField(max_length=20, choices=STATUSES, default='pending')

    # Resolved references
    resolved_channel = models.ForeignKey(
        'TelegramChannel', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='telegram_links'
    )
    resolved_user = models.ForeignKey(
        'TelegramUser', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='telegram_links'
    )

    # For invite links where we can't join but got preview info
    invite_title = models.CharField(max_length=255, blank=True)
    invite_member_count = models.IntegerField(null=True, blank=True)

    # Raw data from Telegram API
    raw_data = models.JSONField(null=True, blank=True)

    error = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"TelegramLink({self.url}) [{self.status}]"


class TgLink(models.Model):
    """
    Resolved invite link destination.
    Keyed on resolved telegram_id so multiple invite hashes pointing to the same
    group/channel collapse into one record.
    """

    STATUS_CHOICES = [
        ('active', 'Active'),
        ('expired', 'Expired'),
        ('invalid', 'Invalid'),
        ('revoked', 'Revoked'),
    ]

    telegram_id = models.BigIntegerField(
        null=True, blank=True, db_index=True,
        help_text='Resolved Telegram ID of the destination (null if preview-only)'
    )
    title = models.CharField(max_length=255)
    username = models.CharField(max_length=255, blank=True, help_text='Public username if available')
    about = models.TextField(blank=True)
    member_count = models.IntegerField(null=True, blank=True)
    is_megagroup = models.BooleanField(default=False)
    is_broadcast = models.BooleanField(default=False)
    is_request_needed = models.BooleanField(default=False, help_text='Join requests required')
    is_verified = models.BooleanField(default=False)
    is_scam = models.BooleanField(default=False)
    is_fake = models.BooleanField(default=False)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')

    # Link to tracked source if destination is already monitored
    channel = models.ForeignKey(
        'TelegramChannel', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='tg_links',
        help_text='Linked TelegramChannel if this destination is a tracked source'
    )

    # User flagging
    flagged = models.BooleanField(default=False, db_index=True)
    flagged_reason = models.TextField(blank=True)

    first_seen = models.DateTimeField(auto_now_add=True)
    last_checked = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = 'TG Link'
        verbose_name_plural = 'TG Links'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['channel']),
            models.Index(fields=['telegram_id', 'status']),
        ]

    def __str__(self):
        return f"{self.title} [{self.status}]"

    @property
    def sighting_count(self):
        return self.events.count()

    @property
    def unique_hashes(self):
        return self.events.values_list('invite_hash', flat=True).distinct()

    @property
    def promoting_channels(self):
        return TelegramChannel.objects.filter(
            tg_link_events__source_link=self
        ).distinct()


class TgLinkEvent(models.Model):
    """
    Each sighting of a t.me invite URL in a message.
    Tracks where and when the invite link was detected, and which account resolved it.
    """

    RESOLUTION_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('resolved', 'Resolved'),
        ('failed', 'Failed'),
        ('expired', 'Expired'),
        ('invalid', 'Invalid'),
    ]

    source_link = models.ForeignKey(
        TgLink, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='events',
        help_text='Resolved destination (set once resolved)'
    )
    entity = models.ForeignKey(
        'MessageEntity',
        on_delete=models.CASCADE, related_name='tg_link_events',
        help_text='The URL entity in the message where this invite link was found'
    )
    channel = models.ForeignKey(
        'TelegramChannel',
        on_delete=models.CASCADE, related_name='tg_link_events',
        help_text='Channel where the invite link was posted (denormalized for fast queries)'
    )

    invite_hash = models.CharField(max_length=255, db_index=True, help_text='Invite hash extracted from URL')
    raw_url = models.URLField(max_length=2000, help_text='Full URL as seen in the message')

    detected_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        TelegramAccount, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='resolved_tg_link_events',
        help_text='Account used to resolve the invite'
    )

    resolution_status = models.CharField(
        max_length=20, choices=RESOLUTION_STATUS_CHOICES, default='pending'
    )
    resolution_error = models.TextField(blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = 'TG Link Event'
        verbose_name_plural = 'TG Link Events'
        ordering = ['-detected_at']
        constraints = [
            models.UniqueConstraint(
                fields=['entity', 'invite_hash'],
                name='unique_entity_invite_hash'
            ),
        ]
        indexes = [
            models.Index(fields=['source_link']),
            models.Index(fields=['channel']),
            models.Index(fields=['resolution_status']),
            models.Index(fields=['invite_hash', 'resolution_status']),
        ]

    def __str__(self):
        return f"TgLinkEvent({self.invite_hash[:12]}) [{self.resolution_status}]"


class UserReaction(models.Model):
    """
    Per-user reaction on a message.
    Populated by periodic batch scan via GetMessagesReactionsRequest.
    Enables bot detection, signal booster identification, and sentiment analysis.
    """

    message = models.ForeignKey(
        'downloads.ArchivedMessage',
        on_delete=models.CASCADE,
        related_name='user_reactions',
    )
    channel = models.ForeignKey(
        'audit.TelegramChannel',
        on_delete=models.CASCADE,
        related_name='user_reactions',
        help_text='Channel where the reaction occurred (denormalized for fast queries)',
    )
    telegram_user_id = models.BigIntegerField(help_text='Telegram user ID of the reactor')
    user = models.ForeignKey(
        'audit.TelegramUser',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='reactions',
        help_text='Linked TelegramUser if tracked',
    )

    # Reaction data
    emoji = models.CharField(max_length=100, help_text='Unicode emoji or custom:ID for custom emoji')
    is_custom_emoji = models.BooleanField(default=False)
    custom_emoji_id = models.BigIntegerField(null=True, blank=True)

    # Timestamps
    reacted_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When the reaction was made (from Telegram if available)',
    )
    detected_at = models.DateTimeField(auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'User Reaction'
        verbose_name_plural = 'User Reactions'
        ordering = ['-detected_at']
        constraints = [
            models.UniqueConstraint(
                fields=['message', 'telegram_user_id', 'emoji'],
                name='unique_user_reaction_per_message',
            ),
        ]
        indexes = [
            models.Index(fields=['telegram_user_id']),
            models.Index(fields=['channel']),
            models.Index(fields=['emoji']),
            models.Index(fields=['channel', 'telegram_user_id']),
            models.Index(fields=['telegram_user_id', 'emoji']),
        ]

    def __str__(self):
        return f"{self.telegram_user_id} reacted {self.emoji} on msg {self.message_id}"


class CIBFlag(models.Model):
    """
    Analyst annotation on a CIB cluster. One row per (cluster_kind, fingerprint);
    re-flagging an existing cluster updates status/note in place.

    Per the design decision, flagged clusters are NOT hidden from results — the
    flag just decorates the cluster row with a 'confirmed' / 'dismissed' badge
    so analysts can see prior judgment without re-investigating.
    """

    KIND_CHOICES = [
        ('text', 'Identical text'),
        ('media', 'Identical media'),
        ('entity', 'Shared URL/entity'),
        ('crossposter', 'Speed crossposter'),
    ]
    STATUS_CHOICES = [
        ('confirmed', 'Confirmed CIB'),
        ('dismissed', 'False positive'),
    ]

    cluster_kind = models.CharField(max_length=20, choices=KIND_CHOICES, db_index=True)
    cluster_fingerprint = models.CharField(max_length=128, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    note = models.TextField(blank=True)
    flagged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='cib_flags',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = 'CIB Flag'
        verbose_name_plural = 'CIB Flags'
        unique_together = [('cluster_kind', 'cluster_fingerprint')]
        indexes = [
            models.Index(fields=['cluster_kind', 'cluster_fingerprint']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"{self.get_cluster_kind_display()} {self.cluster_fingerprint[:12]} ({self.status})"


@receiver(post_save, sender=TelegramChannel)
def create_channel_config(sender, instance, created, **kwargs):
    """
    Auto-create ChannelConfig when a TelegramChannel is created.
    This ensures the listener can immediately process messages from new channels.
    """
    if created:
        ChannelConfig.objects.get_or_create(channel=instance)


@receiver(post_save, sender=ChannelConfig)
def sync_task_priorities_on_config_change(sender, instance, **kwargs):
    """
    When channel config priority or file_type_priority changes,
    update all pending download tasks to use the new priority.
    """
    update_fields = kwargs.get('update_fields')

    # Only sync if priority-related fields changed, or if it's a full save
    if update_fields is None or 'priority' in update_fields or 'file_type_priority' in update_fields:
        instance.sync_pending_task_priorities()
