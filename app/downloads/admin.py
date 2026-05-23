"""
Downloads app admin configuration.
"""

from django.contrib import admin
from django.contrib.postgres.search import SearchQuery
from django.db.models import Q

from .models import ArchivedMessage, DownloadedFile, DownloadTask, TaskRun


@admin.register(DownloadTask)
class DownloadTaskAdmin(admin.ModelAdmin):
    """Admin configuration for DownloadTask."""

    list_display = [
        'id',
        'channel',
        'file_type',
        'original_filename',
        'status',
        'priority',
        'progress',
        'retry_count',
        'created_at',
    ]
    list_filter = ['status', 'file_type', 'priority', 'channel__account']
    search_fields = ['original_filename', 'channel__title', 'telegram_file_id']
    readonly_fields = [
        'id',
        'celery_task_id',
        'created_at',
        'started_at',
        'completed_at',
    ]
    raw_id_fields = ['channel']
    list_select_related = ['channel']

    fieldsets = (
        (None, {
            'fields': ('channel', 'message_id', 'telegram_file_id')
        }),
        ('File Info', {
            'fields': ('original_filename', 'file_type', 'file_size', 'mime_type')
        }),
        ('Status', {
            'fields': ('status', 'priority', 'progress', 'downloaded_bytes', 'download_speed')
        }),
        ('Retry', {
            'fields': ('retry_count', 'max_retries', 'last_error')
        }),
        ('Tracking', {
            'fields': ('celery_task_id', 'created_at', 'started_at', 'completed_at'),
            'classes': ('collapse',)
        }),
    )

    actions = ['pause_downloads', 'resume_downloads', 'retry_downloads']

    @admin.action(description='Pause selected downloads')
    def pause_downloads(self, request, queryset):
        updated = queryset.filter(status__in=['pending', 'downloading']).update(status='paused')
        self.message_user(request, f'Paused {updated} downloads.')

    @admin.action(description='Resume selected downloads')
    def resume_downloads(self, request, queryset):
        updated = queryset.filter(status='paused').update(status='pending')
        self.message_user(request, f'Resumed {updated} downloads.')

    @admin.action(description='Retry selected downloads')
    def retry_downloads(self, request, queryset):
        updated = queryset.filter(status='failed').update(status='pending', retry_count=0, last_error='')
        self.message_user(request, f'Queued {updated} downloads for retry.')


@admin.register(DownloadedFile)
class DownloadedFileAdmin(admin.ModelAdmin):
    """Admin configuration for DownloadedFile."""

    list_display = [
        'original_filename',
        'channel',
        'file_type',
        'file_size_display',
        'is_duplicate',
        'downloaded_at',
    ]
    list_filter = ['file_type', 'is_duplicate', 'channel__account']
    search_fields = ['original_filename', 'sha256_hash', 'channel__title']
    readonly_fields = [
        'id',
        'sha256_hash',
        'downloaded_at',
        'task',
    ]
    raw_id_fields = ['channel', 'task', 'original_file']
    list_select_related = ['channel']

    fieldsets = (
        (None, {
            'fields': ('channel', 'task', 'message_id')
        }),
        ('File Info', {
            'fields': (
                'original_filename',
                'stored_filename',
                'file_path',
                'file_type',
                'file_size',
                'mime_type',
            )
        }),
        ('Media Metadata', {
            'fields': ('media_width', 'media_height', 'media_duration')
        }),
        ('Deduplication', {
            'fields': ('sha256_hash', 'is_duplicate', 'original_file')
        }),
        ('Telegram', {
            'fields': ('telegram_file_id', 'telegram_date')
        }),
        ('Metadata', {
            'fields': ('thumbnail_path', 'downloaded_at'),
            'classes': ('collapse',)
        }),
    )

    def file_size_display(self, obj):
        """Display file size in human readable format."""
        if obj.file_size < 1024:
            return f"{obj.file_size} B"
        elif obj.file_size < 1024 * 1024:
            return f"{obj.file_size / 1024:.1f} KB"
        elif obj.file_size < 1024 * 1024 * 1024:
            return f"{obj.file_size / (1024*1024):.1f} MB"
        return f"{obj.file_size / (1024*1024*1024):.2f} GB"
    file_size_display.short_description = 'Size'


@admin.register(ArchivedMessage)
class ArchivedMessageAdmin(admin.ModelAdmin):
    """Admin configuration for ArchivedMessage."""

    list_display = [
        'id',
        'message_id',
        'channel',
        'sender_name',
        'text_preview',
        'has_media',
        'is_topic_message',
        'views',
        'telegram_date',
    ]
    list_filter = [
        'channel',
        'channel__account',
        'has_media',
        'media_type',
        'is_topic_message',
        'is_pinned',
        'is_post',
        'media_unavailable',
        'is_deleted',
    ]
    # search_fields lists `message_id` purely so Django admin renders the search
    # box. get_search_results() below overrides search entirely and never calls
    # super(), so the default ILIKE-on-cast plan that seq-scans the 6.3M-row
    # table is bypassed.
    search_fields = ['message_id']
    search_help_text = (
        'Numeric Telegram message_id (indexed exact match) or text content '
        '(full-text search via search_vector).'
    )
    readonly_fields = ['id', 'dedup_hash', 'created_at', 'archived_at']
    raw_id_fields = ['channel', 'topic', 'downloaded_file', 'raw_event']
    list_select_related = ['channel', 'topic']

    def get_search_results(self, request, queryset, search_term):
        """
        Index-friendly search.

        * Numeric term: exact equality on message_id (uses an index).
        * Non-numeric term: full-text query via the GIN-indexed search_vector
          (covers the `text` column). Note that this is language-aware tokenized
          search — it won't match substrings inside a word the way ILIKE does.
          For substring searches across messages, use /search/ which is built
          for that.
        """
        use_distinct = False
        if not search_term:
            return queryset, use_distinct

        term = search_term.strip()
        if term.isdigit():
            try:
                return queryset.filter(message_id=int(term)), use_distinct
            except (ValueError, OverflowError):
                return queryset.none(), use_distinct

        # FTS via search_vector (GIN-indexed). Bail out if the term tokenises
        # to nothing (e.g. punctuation only) — SearchQuery would otherwise
        # match everything.
        sq = SearchQuery(term)
        return queryset.filter(search_vector=sq), use_distinct

    fieldsets = (
        (None, {
            'fields': ('id', 'channel', 'message_id', 'dedup_hash')
        }),
        ('Content', {
            'fields': ('text',)
        }),
        ('Sender', {
            'fields': ('sender_id', 'sender_name', 'sender_username')
        }),
        ('Thread/Topic', {
            'fields': ('reply_to_message_id', 'reply_to_top_id', 'is_topic_message', 'topic')
        }),
        ('Media', {
            'fields': (
                'has_media', 'media_type', 'telegram_file_id', 'original_filename',
                'file_size', 'mime_type', 'media_width', 'media_height', 'media_duration',
                'thumbnail_path', 'downloaded_file', 'media_unavailable'
            )
        }),
        ('Message Flags', {
            'fields': ('is_pinned', 'is_post', 'noforwards', 'is_silent', 'is_deleted')
        }),
        ('Grouping', {
            'fields': ('grouped_id', 'post_author', 'ttl_period')
        }),
        ('Engagement', {
            'fields': ('views', 'forwards', 'reactions')
        }),
        ('Timestamps', {
            'fields': ('telegram_date', 'edited_date', 'created_at', 'archived_at')
        }),
        ('Raw Data', {
            'fields': ('raw_event',),
            'classes': ('collapse',)
        }),
    )

    def text_preview(self, obj):
        """Show truncated text preview."""
        if obj.text:
            return obj.text[:100] + '...' if len(obj.text) > 100 else obj.text
        return '-'
    text_preview.short_description = 'Text'


@admin.register(TaskRun)
class TaskRunAdmin(admin.ModelAdmin):
    """Admin configuration for TaskRun."""

    list_display = [
        'id',
        'task_type',
        'channel',
        'status',
        'progress_percent',
        'progress_message',
        'created_at',
    ]
    list_filter = ['task_type', 'status']
    search_fields = ['channel__title', 'error']
    readonly_fields = ['id', 'task_id', 'created_at', 'started_at', 'completed_at']
    raw_id_fields = ['channel', 'account']
    list_select_related = ['channel', 'account']

    fieldsets = (
        (None, {
            'fields': ('task_type', 'task_id', 'channel', 'account')
        }),
        ('Status', {
            'fields': ('status', 'should_cancel', 'progress_percent', 'progress_message', 'progress')
        }),
        ('Error', {
            'fields': ('error',),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'started_at', 'completed_at'),
            'classes': ('collapse',)
        }),
    )
