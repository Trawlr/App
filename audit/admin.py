"""
Audit app admin configuration.
"""

from django.contrib import admin
from django.db.models import Count
from simple_history.admin import SimpleHistoryAdmin

from .models import (
    ActivityLog,
    ChannelConfig,
    ForumTopic,
    ForwardSource,
    GlobalEntity,
    MessageEntity,
    Tag,
    TelegramChannel,
    TelegramReport,
    TelegramUser,
    TgLink,
    TgLinkEvent,
    UserGroupMembership,
    UserMonitoredSource,
    UserNote,
    UserReaction,
)

@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ['name', 'colour', 'description', 'created_at']
    search_fields = ['name']
    list_filter = ['colour']


class ChannelConfigInline(admin.StackedInline):
    """Inline admin for ChannelConfig."""
    model = ChannelConfig
    can_delete = False
    verbose_name_plural = 'Configuration'

    fieldsets = (
        ('Download Settings', {
            'fields': (
                'auto_download_enabled',
                ('download_photos', 'download_videos', 'download_files'),
                'priority',
                'deduplication_mode',
            )
        }),
        ('State', {
            'fields': ('is_paused',)
        }),
        ('Progress', {
            'fields': (
                'last_downloaded_message_id',
                'total_messages',
                'downloaded_messages',
            ),
            'classes': ('collapse',)
        }),
    )


@admin.register(TelegramChannel)
class TelegramChannelAdmin(admin.ModelAdmin):
    """Admin configuration for TelegramChannel."""

    list_display = [
        'title',
        'channel_type',
        'account',
        'username',
        'member_count',
        'is_private',
        'created_at',
    ]
    list_filter = ['channel_type', 'is_private', 'account']
    search_fields = ['title', 'username', 'telegram_id']
    readonly_fields = ['id', 'telegram_id', 'created_at', 'updated_at']
    raw_id_fields = ['account']
    list_select_related = ['account']
    inlines = [ChannelConfigInline]
    filter_horizontal = ['tags']

    fieldsets = (
        (None, {
            'fields': ('telegram_id', 'account', 'title', 'username')
        }),
        ('Details', {
            'fields': ('channel_type', 'is_private', 'member_count', 'avatar')
        }),
        ('Tags', {
            'fields': ('tags',)
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(ChannelConfig)
class ChannelConfigAdmin(admin.ModelAdmin):
    """Admin configuration for ChannelConfig."""

    list_display = [
        'channel',
        'auto_download_enabled',
        'priority',
        'is_paused',
        'download_progress_percent',
    ]
    list_filter = ['auto_download_enabled', 'is_paused', 'priority']
    search_fields = ['channel__title']
    readonly_fields = ['id']
    raw_id_fields = ['channel']
    list_select_related = ['channel']

    fieldsets = (
        (None, {
            'fields': ('channel',)
        }),
        ('Download Settings', {
            'fields': (
                'auto_download_enabled',
                ('download_photos', 'download_videos', 'download_files'),
                'priority',
                'deduplication_mode',
            )
        }),
        ('State', {
            'fields': ('is_paused',)
        }),
        ('Progress', {
            'fields': (
                'last_downloaded_message_id',
                'total_messages',
                'downloaded_messages',
            ),
        }),
    )

    def download_progress_percent(self, obj):
        """Display download progress as percentage."""
        return f"{obj.download_progress_percent}%"
    download_progress_percent.short_description = 'Progress'


class UserGroupMembershipInline(admin.TabularInline):
    """Inline for viewing user's group memberships."""
    model = UserGroupMembership
    extra = 0
    readonly_fields = ['channel', 'first_seen', 'last_seen', 'message_count', 'is_admin']
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(TelegramUser)
class TelegramUserAdmin(SimpleHistoryAdmin):
    """Admin configuration for TelegramUser with history tracking."""

    list_display = [
        'display_name',
        'telegram_id',
        'username',
        'has_bio',
        'is_bot',
        'is_premium',
        'is_blocked',
        'common_chats_count',
        'message_count',
        'full_profile_fetched_at',
        'last_seen',
    ]
    list_filter = [
        'is_bot', 'is_verified', 'is_premium', 'is_scam', 'is_fake',
        'is_blocked', 'phone_calls_available', 'video_calls_available',
        'voice_messages_forbidden', 'contact_require_premium',
        'has_pinned_stories', 'has_scheduled_messages',
    ]
    search_fields = ['telegram_id', 'username', 'first_name', 'last_name', 'phone', 'bio']
    readonly_fields = [
        'telegram_id', 'first_seen', 'last_seen', 'message_count',
        'full_profile_fetched_at', 'access_hash',
    ]
    inlines = [UserGroupMembershipInline]
    filter_horizontal = ['tags']

    fieldsets = (
        (None, {
            'fields': ('telegram_id', 'first_name', 'last_name', 'username', 'phone')
        }),
        ('Bio & Personal', {
            'fields': ('bio', 'birthday', 'private_forward_name', 'personal_channel_id'),
            'description': 'Personal information from full profile fetch'
        }),
        ('Flags', {
            'fields': (
                ('is_bot', 'is_verified', 'is_premium'),
                ('is_scam', 'is_fake', 'is_restricted'),
                ('is_deleted', 'is_support'),
            )
        }),
        ('Contact & Communication', {
            'fields': (
                ('is_contact', 'is_mutual_contact', 'is_close_friend'),
                ('phone_calls_available', 'video_calls_available'),
                ('voice_messages_forbidden', 'contact_require_premium'),
                'is_blocked',
            )
        }),
        ('Business Profile', {
            'fields': ('business_intro', 'business_location', 'business_work_hours'),
            'classes': ('collapse',)
        }),
        ('Activity Indicators', {
            'fields': (
                'common_chats_count',
                ('has_pinned_stories', 'has_scheduled_messages'),
                'pinned_message_id',
            )
        }),
        ('Profile Photo', {
            'fields': ('photo_id', 'photo_path', 'profile_photo_updated_at'),
            'classes': ('collapse',)
        }),
        ('Tracking', {
            'fields': (
                ('first_seen', 'last_seen'),
                'message_count',
                'full_profile_fetched_at',
                'access_hash',
            )
        }),
        ('Flagging', {
            'fields': (
                'is_flagged',
                ('flagged_reason', 'flagged_at'),
                'flagged_notes',
                ('reported_to_telegram', 'reported_at'),
            ),
            'classes': ('collapse',)
        }),
        ('Investigation', {
            'fields': ('notes', 'tags')
        }),
    )

    @admin.display(boolean=True, description='Bio')
    def has_bio(self, obj):
        return bool(obj.bio)


@admin.register(UserGroupMembership)
class UserGroupMembershipAdmin(SimpleHistoryAdmin):
    """Admin configuration for UserGroupMembership with history tracking."""

    list_display = [
        'user',
        'channel',
        'is_admin',
        'is_creator',
        'message_count',
        'last_seen',
    ]
    list_filter = ['is_admin', 'is_creator', 'channel']
    search_fields = ['user__username', 'user__first_name', 'channel__title']
    readonly_fields = ['id', 'first_seen', 'last_seen', 'message_count']
    raw_id_fields = ['user', 'channel']
    list_select_related = ['user', 'channel']

    fieldsets = (
        (None, {
            'fields': ('user', 'channel')
        }),
        ('Role', {
            'fields': (('is_admin', 'is_creator'), 'admin_title')
        }),
        ('Activity', {
            'fields': ('first_seen', 'last_seen', 'last_message_date', 'message_count')
        }),
    )


@admin.register(TelegramReport)
class TelegramReportAdmin(admin.ModelAdmin):
    """Admin configuration for TelegramReport."""

    list_display = [
        'id',
        'report_type',
        'channel',
        'reason',
        'success',
        'account',
        'created_at',
    ]
    list_filter = ['reason', 'report_type', 'success']
    search_fields = ['reported_name', 'message']
    readonly_fields = ['id', 'created_at']
    raw_id_fields = ['channel', 'user', 'account']
    list_select_related = ['channel', 'account']

    fieldsets = (
        (None, {
            'fields': ('report_type', 'channel', 'user', 'account')
        }),
        ('Report Details', {
            'fields': ('reason', 'message', 'reported_telegram_id', 'reported_message_id', 'reported_name')
        }),
        ('Result', {
            'fields': ('success', 'error_message', 'created_at')
        }),
    )


@admin.register(MessageEntity)
class MessageEntityAdmin(admin.ModelAdmin):
    """Admin configuration for MessageEntity."""

    list_display = [
        'entity_type_display',
        'text_display',
        'url_display',
        'message',
    ]
    list_filter = ['entity__entity_type']
    search_fields = ['entity__text', 'entity__url']
    readonly_fields = ['id']
    raw_id_fields = ['message', 'entity']
    list_select_related = ['message', 'entity']
    ordering = ['-id']

    @admin.display(description='Type', ordering='entity__entity_type')
    def entity_type_display(self, obj):
        return obj.entity.entity_type

    @admin.display(description='Text', ordering='entity__text')
    def text_display(self, obj):
        return obj.entity.text

    @admin.display(description='URL', ordering='entity__url')
    def url_display(self, obj):
        return obj.entity.url

    def get_search_results(self, request, queryset, search_term):
        queryset, use_distinct = super().get_search_results(request, queryset, search_term)
        # Also search by message_id (integer)
        if search_term.isdigit():
            queryset |= self.model.objects.filter(message__message_id=int(search_term))
        return queryset, use_distinct


@admin.register(GlobalEntity)
class GlobalEntityAdmin(admin.ModelAdmin):
    """Admin configuration for GlobalEntity (the deduplicated entity table)."""

    list_display = ['entity_type', 'text', 'url', 'user_id', 'occurrence_count', 'first_seen_at', 'last_seen_at']
    list_filter = ['entity_type']
    search_fields = ['text', 'url']
    readonly_fields = ['id', 'content_hash', 'first_seen_at', 'last_seen_at']
    ordering = ['-last_seen_at']

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_occurrences=Count('occurrences'))

    @admin.display(description='Occurrences', ordering='_occurrences')
    def occurrence_count(self, obj):
        return obj._occurrences


@admin.register(ForwardSource)
class ForwardSourceAdmin(admin.ModelAdmin):
    """Admin configuration for ForwardSource."""

    list_display = [
        'id',
        'source_type',
        'source_title',
        'source_telegram_id',
        'message',
        'original_date',
    ]
    list_filter = ['source_type']
    search_fields = ['source_title', 'source_username']
    readonly_fields = ['id', 'original_date']
    raw_id_fields = ['message']
    list_select_related = ['message']


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    """Admin configuration for ActivityLog."""

    list_display = [
        'activity_type',
        'source',
        'channel',
        'description_preview',
        'timestamp',
    ]
    list_filter = ['activity_type', 'source']
    search_fields = ['description', 'channel__title']
    readonly_fields = ['id', 'timestamp']
    raw_id_fields = ['channel', 'telegram_user']
    list_select_related = ['channel']

    def description_preview(self, obj):
        """Show truncated description preview."""
        if obj.description:
            return obj.description[:80] + '...' if len(obj.description) > 80 else obj.description
        return '-'
    description_preview.short_description = 'Description'


@admin.register(UserMonitoredSource)
class UserMonitoredSourceAdmin(admin.ModelAdmin):
    """Admin configuration for UserMonitoredSource (pinned sources)."""

    list_display = ['user', 'channel', 'created_at']
    list_filter = ['user', 'created_at']
    search_fields = ['user__username', 'channel__title']
    readonly_fields = ['id', 'created_at']
    raw_id_fields = ['user', 'channel']
    list_select_related = ['user', 'channel']


@admin.register(ForumTopic)
class ForumTopicAdmin(SimpleHistoryAdmin):
    """Admin configuration for ForumTopic."""

    list_display = ['title', 'channel', 'topic_id', 'is_general', 'is_pinned', 'is_closed', 'message_count', 'updated_at']
    list_filter = ['is_general', 'is_pinned', 'is_closed', 'is_hidden', 'channel']
    search_fields = ['title', 'channel__title']
    readonly_fields = ['id', 'created_at', 'updated_at']
    raw_id_fields = ['channel']
    list_select_related = ['channel']
    ordering = ['channel', 'title']

    fieldsets = (
        (None, {
            'fields': ('channel', 'topic_id', 'title')
        }),
        ('Icon', {
            'fields': ('icon_color', 'icon_emoji_id'),
            'classes': ('collapse',)
        }),
        ('State', {
            'fields': ('is_general', 'is_pinned', 'is_closed', 'is_hidden')
        }),
        ('Stats', {
            'fields': ('message_count',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(TgLink)
class TgLinkAdmin(SimpleHistoryAdmin):
    list_display = ['title', 'telegram_id', 'username', 'member_count', 'status', 'flagged', 'sighting_count', 'last_checked', 'first_seen']
    list_filter = ['status', 'flagged', 'is_megagroup', 'is_broadcast', 'is_scam', 'is_fake', 'is_verified']
    search_fields = ['title', 'username', 'about']
    readonly_fields = ['first_seen', 'last_checked', 'created_at', 'updated_at']
    raw_id_fields = ['channel']
    ordering = ['-created_at']

    def sighting_count(self, obj):
        return obj.sighting_count
    sighting_count.short_description = 'Sightings'


@admin.register(TgLinkEvent)
class TgLinkEventAdmin(SimpleHistoryAdmin):
    list_display = ['invite_hash_short', 'channel', 'source_link', 'resolution_status', 'detected_at', 'resolved_at', 'resolved_by']
    list_filter = ['resolution_status', 'channel']
    search_fields = ['invite_hash', 'raw_url']
    readonly_fields = ['detected_at', 'resolved_at']
    raw_id_fields = ['source_link', 'entity', 'channel', 'resolved_by']
    ordering = ['-detected_at']

    def invite_hash_short(self, obj):
        return obj.invite_hash[:16] + '...' if len(obj.invite_hash) > 16 else obj.invite_hash
    invite_hash_short.short_description = 'Invite Hash'


@admin.register(UserReaction)
class UserReactionAdmin(admin.ModelAdmin):
    list_display = ['telegram_user_id', 'emoji', 'channel', 'message_id_display', 'reacted_at', 'detected_at']
    list_filter = ['channel', 'is_custom_emoji', 'emoji']
    search_fields = ['telegram_user_id', 'emoji']
    raw_id_fields = ['message', 'channel', 'user']
    readonly_fields = ['detected_at', 'created_at', 'updated_at']
    ordering = ['-detected_at']

    def message_id_display(self, obj):
        return obj.message.message_id if obj.message else '-'
    message_id_display.short_description = 'Message ID'


@admin.register(UserNote)
class UserNoteAdmin(SimpleHistoryAdmin):
    """Admin configuration for UserNote with history tracking."""

    list_display = ['id', 'content_type', 'object_id', 'text_preview', 'created_by', 'created_at', 'is_edited']
    list_filter = ['content_type', 'created_by', 'created_at']
    search_fields = ['text', 'created_by__username']
    readonly_fields = ['id', 'created_at', 'updated_at']
    raw_id_fields = ['created_by']
    list_select_related = ['content_type', 'created_by']
    ordering = ['-created_at']

    fieldsets = (
        (None, {
            'fields': ('content_type', 'object_id', 'text')
        }),
        ('Metadata', {
            'fields': ('created_by', 'created_at', 'updated_at')
        }),
    )

    def text_preview(self, obj):
        """Show truncated text preview."""
        if obj.text:
            return obj.text[:60] + '...' if len(obj.text) > 60 else obj.text
        return '-'
    text_preview.short_description = 'Text'

    @admin.display(boolean=True, description='Edited')
    def is_edited(self, obj):
        return obj.is_edited
