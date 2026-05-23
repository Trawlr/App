from django.shortcuts import get_object_or_404
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from audit.models import Tag, TelegramChannel, ChannelConfig, UserMonitoredSource
from api.serializers.channels import (
    TelegramChannelListSerializer,
    TelegramChannelDetailSerializer
)
from api.serializers.tags import TagCompactSerializer
from api.filters import TelegramChannelFilter

class TelegramChannelViewSet(viewsets.ReadOnlyModelViewSet):
    """
    View the telegram channels, groups and chats that are onboarded to Trawlr.
    """
    permission_classes = [IsAuthenticated]
    filterset_class = TelegramChannelFilter
    search_fields = ['title']
    ordering = ['-joined_at']

    def get_queryset(self):
        return TelegramChannel.objects.from_active_accounts().select_related(
            'account', 'config'
        ).prefetch_related('tags')

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return TelegramChannelDetailSerializer
        return TelegramChannelListSerializer

    def _get_channel_by_telegram_id(self, telegram_id):
        """Helper to get channel by telegram_id"""
        return get_object_or_404(
            self.get_queryset(),
            telegram_id=telegram_id
        )

    @action(detail=False, methods=['get'], url_path='tgid/(?P<telegram_id>[0-9-]+)')
    def by_telegram_id(self, request, telegram_id=None):
        """Get channel detail by Telegram ID."""
        channel = self._get_channel_by_telegram_id(telegram_id)
        serializer = TelegramChannelDetailSerializer(channel)
        return Response(serializer.data)

    @action(detail=True, methods=['post'], url_path='tags/add')
    def add_tags(self, request, pk=None):
        """Add tags to a channel. Expects {"tags": [1, 2]} (tag IDs)."""
        channel = self.get_object()
        tag_ids = request.data.get('tags', [])
        tags = Tag.objects.filter(id__in=tag_ids)
        channel.tags.add(*tags)
        return Response(TagCompactSerializer(channel.tags.all(), many=True).data)

    @action(detail=True, methods=['post'], url_path='tags/remove')
    def remove_tags(self, request, pk=None):
        """Remove tags from a channel. Expects {"tags": [1, 2]} (tag IDs)."""
        channel = self.get_object()
        tag_ids = request.data.get('tags', [])
        channel.tags.remove(*tag_ids)
        return Response(TagCompactSerializer(channel.tags.all(), many=True).data)
