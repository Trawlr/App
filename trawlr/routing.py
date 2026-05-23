"""
WebSocket URL routing for trawlr project.
"""

from django.urls import path

from downloads.consumers import DownloadProgressConsumer

websocket_urlpatterns = [
    path('ws/downloads/', DownloadProgressConsumer.as_asgi()),
]
