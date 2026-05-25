"""
Audit app URL configuration (Sources/Channels).
"""

from django.urls import path

from . import views

app_name = 'audit'

urlpatterns = [
    # Telegram Users (global view)
    path('users/', views.users, name='users'),
    path('users/scan-all/', views.scan_all_members, name='scan_all_members'),
    path('user/<int:pk>/', views.user_detail, name='user_detail'),
    path('user/<int:pk>/posts/', views.user_posts, name='user_posts'),
    path('user/<int:pk>/activity/', views.user_activity, name='user_activity'),
    path('user/<int:pk>/network/', views.user_network, name='user_network'),

    # Sources (channels/groups)
    path('sources/', views.sources, name='sources'),
    path('sources/archived/', views.archived_sources, name='archived_sources'),
    path('sources/check-availability/', views.check_sources_availability, name='check_sources_availability'),
    path('feed/', views.feed, name='feed'),
    path('monitored/', views.monitored_feed, name='monitored_feed'),
    path('source/<int:pk>/', views.source_detail, name='source_detail'),
    path('source/<int:pk>/config/', views.source_config, name='source_config'),
    path('source/<int:pk>/notes/', views.source_notes, name='source_notes'),
    path('source/<int:pk>/notes/add/', views.source_notes_add, name='source_notes_add'),
    path('source/<int:pk>/notes/<int:note_pk>/edit/', views.source_notes_edit, name='source_notes_edit'),
    path('source/<int:pk>/notes/<int:note_pk>/delete/', views.source_notes_delete, name='source_notes_delete'),
    path('source/<int:pk>/files/', views.source_files, name='source_files'),
    path('source/<int:pk>/files/gallery/', views.source_files_gallery, name='source_files_gallery'),
    path('source/<int:pk>/posts/', views.source_posts, name='source_posts'),
    path('source/<int:pk>/posts/v2/', views.source_posts_v2, name='source_posts_v2'),
    path('source/<int:pk>/posts/v3/', views.source_posts_v3, name='source_posts_v3'),
    path('source/<int:pk>/posts/feed/', views.source_posts_feed, name='source_posts_feed'),
    path('source/<int:pk>/users/', views.source_users, name='source_users'),
    path('source/<int:pk>/activity/', views.source_activity, name='source_activity'),
    path('source/<int:pk>/scan/', views.source_scan, name='source_scan'),
    path('source/<int:pk>/scan-members/', views.source_scan_members, name='source_scan_members'),
    path('source/<int:pk>/fetch-profiles/', views.source_fetch_profiles, name='source_fetch_profiles'),
    path('source/<int:pk>/backfill-thumbnails/', views.source_backfill_thumbnails, name='source_backfill_thumbnails'),
    path('source/<int:pk>/sync-missing-media/', views.source_sync_missing_media, name='source_sync_missing_media'),
    path('source/<int:pk>/pause/', views.source_pause, name='source_pause'),
    path('source/<int:pk>/toggle-auto/', views.source_toggle_auto, name='source_toggle_auto'),
    path('source/<int:pk>/toggle-monitor/', views.source_toggle_monitor, name='source_toggle_monitor'),
    path('source/<int:pk>/check-availability/', views.check_source_availability, name='check_source_availability'),
    path('source/<int:pk>/refresh-counts/', views.refresh_media_counts, name='refresh_media_counts'),
    path('source/<int:pk>/tasks-partial/', views.source_tasks, name='source_tasks'),
    path('source/<int:pk>/leave/', views.leave_source, name='leave_source'),
    path('source/<int:pk>/archive/', views.archive_source, name='archive_source'),
    path('source/<int:pk>/unarchive/', views.unarchive_source, name='unarchive_source'),

    # Source tags
    path('source/<int:pk>/tags/', views.source_tags, name='source_tags'),
    path('source/<int:pk>/tags/add/', views.source_add_tag, name='source_add_tag'),
    path('source/<int:pk>/tags/<int:tag_pk>/remove/', views.source_remove_tag, name='source_remove_tag'),

    # Post thumbnails and downloads
    path('post/<int:pk>/thumbnail/', views.serve_post_thumbnail, name='serve_post_thumbnail'),
    path('post/<int:pk>/download/', views.queue_post_download, name='queue_post_download'),
    path('post/<int:pk>/export/', views.export_post_json, name='export_post_json'),
    path('post/<int:pk>/history/', views.post_history, name='post_history'),

    # Reporting
    path('source/<int:pk>/report/', views.report_source, name='report_source'),
    path('user/<int:pk>/report/', views.report_user, name='report_user'),
    path('source/<int:source_pk>/post/<int:message_id>/report/', views.report_post, name='report_post'),

    # User notes
    path('user/<int:pk>/notes/', views.user_notes, name='user_notes'),
    path('user/<int:pk>/notes/add/', views.user_notes_add, name='user_notes_add'),
    path('user/<int:pk>/notes/<int:note_pk>/edit/', views.user_notes_edit, name='user_notes_edit'),
    path('user/<int:pk>/notes/<int:note_pk>/delete/', views.user_notes_delete, name='user_notes_delete'),

    # User tags
    path('user/<int:pk>/tags/', views.user_tags, name='user_tags'),
    path('user/<int:pk>/tags/add/', views.user_add_tag, name='user_add_tag'),
    path('user/<int:pk>/tags/<int:tag_pk>/remove/', views.user_remove_tag, name='user_remove_tag'),

    # User flagging and profile photo
    path('user/<int:pk>/flag/', views.flag_user, name='flag_user'),
    path('user/<int:pk>/unflag/', views.unflag_user, name='unflag_user'),
    path('user/<int:pk>/profile-photo/', views.user_profile_photo, name='user_profile_photo'),
    path('user/<int:pk>/fetch-profile/', views.fetch_user_profile, name='fetch_user_profile'),
    path('user/<int:pk>/delete-media/', views.delete_user_media, name='delete_user_media'),

    # Activity dashboard
    path('activity/', views.activity, name='activity'),

    # Task management
    path('tasks/', views.tasks, name='tasks'),
    path('tasks/<int:pk>/cancel/', views.cancel_task, name='cancel_task'),
    path('tasks/cleanup/', views.cleanup_tasks, name='cleanup_tasks'),
]
