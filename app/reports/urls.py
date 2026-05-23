from django.urls import path
from . import views, views_activity, views_cib, views_user_network

app_name = 'reports'

urlpatterns = [
    # HTML Views
    path('', views.index, name='index'),
    path('content/', views.content_analytics, name='content'),
    path('users/', views.user_reports, name='users'),
    path('sources/', views.source_analytics, name='sources'),
    path('media-inventory/', views.media_inventory, name='media_inventory'),
    path('investigation/', views.investigation, name='investigation'),

    # API Endpoints (JSON for charts)
    path('api/content/volume/', views.api_content_volume, name='api_content_volume'),
    path('api/content/media/', views.api_content_media, name='api_content_media'),
    path('api/content/entities/', views.api_content_entities, name='api_content_entities'),
    path('api/content/forwards/', views.api_content_forwards, name='api_content_forwards'),
    path('api/users/activity/', views.api_users_activity, name='api_users_activity'),
    path('api/users/churn/', views.api_users_churn, name='api_users_churn'),
    path('api/users/top-posters/', views.api_users_top_posters, name='api_users_top_posters'),
    path('api/sources/health/', views.api_sources_health, name='api_sources_health'),
    path('api/sources/volume/', views.api_sources_volume, name='api_sources_volume'),
    path('api/media-inventory/trend/', views.api_media_inventory_trend, name='api_media_inventory_trend'),
    path('api/investigation/reports/', views.api_investigation_reports, name='api_investigation_reports'),

    # Activity Map API (used by source/user/aggregate Activity tabs)
    path('api/activity/', views_activity.api_activity, name='api_activity'),

    # CIB detector (Coordinated Inauthentic Behavior) — used by /map/cib/
    path('api/cib/', views_cib.api_cib, name='api_cib'),
    path('api/cib/flag/', views_cib.api_cib_flag, name='api_cib_flag'),
    path('api/cib/flag/<int:flag_id>/', views_cib.api_cib_flag, name='api_cib_flag_delete'),

    # Per-user Network tab (audit/user/<pk>/network/) — three panels
    path('api/user/<int:pk>/lanes/', views_user_network.api_user_lanes, name='api_user_lanes'),
    path('api/user/<int:pk>/flow/', views_user_network.api_user_flow, name='api_user_flow'),
    path('api/user/<int:pk>/copost/', views_user_network.api_user_copost, name='api_user_copost'),

    # Export Endpoints
    path('content/export/', views.export_content, name='export_content'),
    path('users/export/', views.export_users, name='export_users'),
    path('sources/export/', views.export_sources, name='export_sources'),
    path('investigation/export/', views.export_investigation, name='export_investigation'),
]
