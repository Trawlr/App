"""
Accounts app URL configuration.
"""

from django.urls import path

from . import views

app_name = 'accounts'

urlpatterns = [
    # Telegram accounts
    path('accounts/', views.telegram_accounts, name='telegram_accounts'),
    path('account/add/', views.telegram_account_add, name='telegram_account_add'),
    path('account/<int:pk>/', views.telegram_account_detail, name='telegram_account_detail'),
    path('account/<int:pk>/auth/', views.telegram_account_auth, name='telegram_account_auth'),
    path('account/<int:pk>/delete/', views.telegram_account_delete, name='telegram_account_delete'),
    path('account/<int:pk>/sync/', views.telegram_account_sync, name='telegram_account_sync'),
    path('account/<int:pk>/listeners/', views.telegram_account_listeners, name='telegram_account_listeners'),
    path('account/<int:pk>/listener/start/', views.telegram_listener_start, name='telegram_listener_start'),
    path('account/<int:pk>/listener/stop/', views.telegram_listener_stop, name='telegram_listener_stop'),
    path('account/<int:pk>/join/', views.telegram_account_join_channel, name='telegram_account_join_channel'),
    path('join/', views.join_channel, name='join_channel'),

    # Settings
    path('settings/', views.settings, name='settings'),
    path('settings/clear-queue/', views.clear_download_queue, name='clear_download_queue'),

    # API Token Management
    path('settings/api-token/create/', views.api_token_create, name='api_token_create'),
    path('settings/api-token/regenerate/', views.api_token_regenerate, name='api_token_regenerate'),
    path('settings/api-token/delete/', views.api_token_delete, name='api_token_delete'),

    # Exclusion list management
    path('settings/exclusions/', views.exclusion_list, name='exclusion_list'),
    path('settings/exclusions/add/', views.exclusion_add, name='exclusion_add'),
    path('settings/exclusions/<int:pk>/toggle/', views.exclusion_toggle, name='exclusion_toggle'),
    path('settings/exclusions/<int:pk>/delete/', views.exclusion_delete, name='exclusion_delete'),

    # API endpoints for search autocomplete
    path('api/search-users/', views.search_users_api, name='search_users_api'),
    path('api/search-sources/', views.search_sources_api, name='search_sources_api'),

    # Task queue management (used by audit:tasks page)
    path('tasks/clear/', views.clear_task_runs, name='clear_task_runs'),

    # Manual task invocation (from settings page)
    path('settings/invoke/process-downloads/', views.invoke_process_downloads, name='invoke_process_downloads'),
    path('settings/invoke/sync-channels/', views.invoke_sync_channels, name='invoke_sync_channels'),
    path('settings/invoke/refresh-stats/', views.invoke_refresh_stats, name='invoke_refresh_stats'),
    path('settings/invoke/refresh-media-counts/', views.invoke_refresh_media_counts, name='invoke_refresh_media_counts'),
    path('settings/invoke/recover-stuck-tasks/', views.invoke_recover_stuck_tasks, name='invoke_recover_stuck_tasks'),
    path('settings/invoke/check-availability/', views.invoke_check_availability, name='invoke_check_availability'),
    path('settings/invoke/sync-forum-topics/', views.invoke_sync_forum_topics, name='invoke_sync_forum_topics'),

    # Dead letter queue management
    path('settings/dead-letters/stats/', views.get_dead_letters_stats, name='get_dead_letters_stats'),
    path('settings/invoke/requeue-dead-letters/', views.invoke_requeue_dead_letters, name='invoke_requeue_dead_letters'),
    path('settings/invoke/purge-dead-letters/', views.invoke_purge_dead_letters, name='invoke_purge_dead_letters'),
]
