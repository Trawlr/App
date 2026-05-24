from django.contrib import admin

from .models import NotificationDelivery, WatchlistEntry


@admin.register(WatchlistEntry)
class WatchlistEntryAdmin(admin.ModelAdmin):
    list_display = ('name', 'mode', 'entity_type', 'entity_value', 'target_type', 'is_active', 'trigger_count', 'last_triggered_at')
    list_filter = ('is_active', 'mode', 'entity_type', 'target_type')
    search_fields = ('name', 'description', 'entity_value')
    readonly_fields = ('trigger_count', 'last_triggered_at', 'created_at', 'updated_at')


@admin.register(NotificationDelivery)
class NotificationDeliveryAdmin(admin.ModelAdmin):
    list_display = ('id', 'entry', 'status', 'attempts', 'created_at', 'delivered_at')
    list_filter = ('status',)
    readonly_fields = ('entry', 'event_payload', 'match_context', 'attempts',
                       'last_attempt_at', 'last_error', 'delivered_at', 'created_at')
    search_fields = ('entry__name', 'last_error')
