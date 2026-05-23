"""
Dashboard app URL configuration.
"""

from django.urls import path

from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.index, name='index'),
    path('api/activity/', views.activity_data, name='activity_data'),
    path('api/messages-realtime/', views.messages_realtime_data, name='messages_realtime_data'),
    path('api/total-activity/', views.total_activity_data, name='total_activity_data'),
    # HTMX partials for lazy-loading
    path('partials/messages/', views.stats_messages, name='stats_messages'),
    path('partials/downloads/', views.stats_downloads, name='stats_downloads'),
    path('partials/secondary/', views.stats_secondary, name='stats_secondary'),
    path('partials/queue/', views.stats_queue, name='stats_queue'),
    path('partials/media/', views.stats_media_breakdown, name='stats_media_breakdown'),
    path('partials/accounts/', views.stats_accounts, name='stats_accounts'),
    path('partials/core-tasks/', views.stats_core_tasks, name='stats_core_tasks'),
]
