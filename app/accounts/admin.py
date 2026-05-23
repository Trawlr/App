"""
Accounts app admin configuration.
"""

from django.contrib import admin

from .models import GlobalSettings, TelegramAccount

@admin.register(TelegramAccount)
class TelegramAccountAdmin(admin.ModelAdmin):
    """Admin configuration for TelegramAccount."""

    list_display = [
        'phone_number',
        'user',
        'is_active',
        'is_authenticated',
        'listener_status',
        'max_concurrent_downloads',
        'created_at',
    ]
    list_filter = ['is_active', 'is_authenticated', 'listener_status']
    search_fields = ['phone_number', 'user__username']
    readonly_fields = ['id', 'created_at', 'updated_at']
    raw_id_fields = ['user']
    list_select_related = ['user']

    fieldsets = (
        (None, {
            'fields': ('user', 'phone_number', 'api_id', 'api_hash')
        }),
        ('Status', {
            'fields': ('is_active', 'is_authenticated', 'two_factor_enabled')
        }),
        ('Listener', {
            'fields': ('listener_status', 'listener_started_at', 'listener_error')
        }),
        ('Rate Limiting', {
            'fields': ('flood_wait_until',)
        }),
        ('Settings', {
            'fields': ('max_concurrent_downloads', 'process_events')
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(GlobalSettings)
class GlobalSettingsAdmin(admin.ModelAdmin):
    """Admin configuration for GlobalSettings (singleton)."""

    list_display = ['__str__', 'storage_root', 'filename_format', 'default_retry_count']
    readonly_fields = ['id']

    fieldsets = (
        ('Storage', {
            'fields': ('storage_root', 'filename_format')
        }),
        ('Download Defaults', {
            'fields': ('default_retry_count',)
        }),
        ('Scheduler Intervals', {
            'fields': (
                'download_queue_interval',
                'channel_sync_interval',
                'channel_stats_interval',
                'media_counts_interval',
                'reaction_scan_interval',
            )
        }),
        ('Event Processing', {
            'fields': (
                'event_processing_enabled',
                'event_processor_batch_size',
                'event_processor_retry_count',
                'event_processor_retry_backoff_min',
                'event_processor_retry_backoff_max',
                'tglink_resolution',
                'store_raw_events',
                'stream_raw_events',
            )
        }),
    )

    def has_add_permission(self, request):
        # Only allow one instance otherwise bad shit happens
        return not GlobalSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        # Prevent deletion because ppl are idiots
        return False
