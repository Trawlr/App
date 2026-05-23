"""Ops panel URL configuration."""

from django.urls import path

from . import views

app_name = 'ops'

urlpatterns = [
    path('queues/', views.queues_index, name='queues'),
    path('queues/<str:name>/peek/', views.queue_peek, name='queue_peek'),
    path('queues/<str:name>/purge/', views.queue_purge, name='queue_purge'),
    path('queues/<str:name>/evict-purge/', views.queue_evict_and_purge, name='queue_evict_purge'),
    path('connections/close/', views.connection_close, name='connection_close'),
]
