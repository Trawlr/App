"""
Map app URL configuration.
Each map type has its own page.
"""

from django.urls import path

from . import views

app_name = 'graph'

urlpatterns = [
    # Index - overview of all map types
    path('', views.index, name='index'),

    # Individual map pages
    path('forwards/', views.map_forwards, name='forwards'),
    path('users/', views.map_users, name='users'),
    path('crosspost/', views.map_crosspost, name='crosspost'),
    path('urls/', views.map_urls, name='urls'),
    path('media/', views.map_media, name='media'),
    path('mentions/', views.map_mentions, name='mentions'),
    path('admins/', views.map_admins, name='admins'),
    path('activity/', views.map_activity, name='activity'),
    path('cib/', views.map_cib, name='cib'),

    # API endpoints for each map type
    path('api/forwards/', views.api_forward_network, name='api_forwards'),
    path('api/users/', views.api_user_network, name='api_users'),
    path('api/crosspost/', views.api_crosspost_network, name='api_crosspost'),
    path('api/urls/', views.api_url_network, name='api_urls'),
    path('api/domains/', views.api_domain_list, name='api_domains'),
    path('api/media/', views.api_media_network, name='api_media'),
    path('api/mentions/', views.api_mention_network, name='api_mentions'),
    path('api/admins/', views.api_admin_network, name='api_admins'),
]
