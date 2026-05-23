"""
Downloads app URL configuration.
"""

from django.urls import path

from . import views

app_name = 'downloads'

urlpatterns = [
    # Download queue
    path('downloads/', views.index, name='index'),
    path('downloads/queue/', views.queue, name='queue'),

    # Individual download actions
    path('download/<int:pk>/pause/', views.pause, name='pause'),
    path('download/<int:pk>/resume/', views.resume, name='resume'),
    path('download/<int:pk>/cancel/', views.cancel, name='cancel'),
    path('download/<int:pk>/retry/', views.retry, name='retry'),

    # Bulk actions
    path('downloads/pause-all/', views.pause_all, name='pause_all'),
    path('downloads/resume-all/', views.resume_all, name='resume_all'),
    path('downloads/clear-completed/', views.clear_completed, name='clear_completed'),
    path('downloads/retry-all-failed/', views.retry_all_failed, name='retry_all_failed'),

    # File browser
    path('files/', views.files, name='files'),
    path('file/<int:pk>/', views.file_detail, name='file_detail'),
    path('file/<int:pk>/view/', views.file_view, name='file_view'),
    path('file/<int:pk>/delete/', views.file_delete, name='file_delete'),

    # Media serving
    path('media/<int:pk>/', views.serve_media, name='serve_media'),
    path('thumbnail/<int:pk>/', views.serve_thumbnail, name='serve_thumbnail'),

    # Archived message actions
    path('downloads/archived-message/<int:pk>/delete-thumbnail/', views.archived_message_delete_thumbnail, name='archived_message_delete_thumbnail'),

]
