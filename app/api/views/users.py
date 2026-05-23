from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from audit.models import Tag, TelegramUser, UserGroupMembership
from downloads.models import ArchivedMessage
from api.serializers.users import (
    TelegramUserListSerializer,
    TelegramUserDetailSerializer,
    UserGroupMembershipSerializer
)
from api.serializers.messages import ArchivedMessageListSerializer
from api.serializers.tags import TagCompactSerializer
from api.filters import TelegramUserFilter
from django_filters.rest_framework import DjangoFilterBackend


class TelegramUserViewSet(viewsets.ReadOnlyModelViewSet):
    """
    These endpoints return Telegram users that are being tracked by Trawlr.
    """
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = TelegramUserFilter
    ordering = ['-last_seen']

    def get_queryset(self):
        return TelegramUser.objects.all().distinct()

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return TelegramUserDetailSerializer
        return TelegramUserListSerializer

    @action(detail=True, methods=['get'])
    def memberships(self, request, pk=None):
        """Get all group memberships for this user across all sources."""
        user = self.get_object()
        memberships = user.memberships.all().select_related('channel')
        serializer = UserGroupMembershipSerializer(memberships, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def messages(self, request, pk=None):
        """Get all messages sent by this user across all sources."""
        user = self.get_object()
        messages = ArchivedMessage.objects.from_active_accounts().filter(
            sender_id=user.telegram_id
        ).select_related('channel').order_by('-telegram_date')

        page = self.paginate_queryset(messages)
        if page is not None:
            serializer = ArchivedMessageListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = ArchivedMessageListSerializer(messages, many=True)
        return Response(serializer.data)

    def _get_user_by_telegram_id(self, telegram_id):
        """Helper to get user by telegram_id"""
        return get_object_or_404(
            self.get_queryset(),
            telegram_id=telegram_id
        )

    @action(detail=False, methods=['get'], url_path='tgid/(?P<telegram_id>[0-9]+)')
    def by_telegram_id(self, request, telegram_id=None):
        """Get user detail by Telegram ID."""
        user = self._get_user_by_telegram_id(telegram_id)
        serializer = TelegramUserDetailSerializer(user)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='tgid/(?P<telegram_id>[0-9]+)/memberships')
    def memberships_by_telegram_id(self, request, telegram_id=None):
        """Get all group memberships for user by Telegram ID."""
        user = self._get_user_by_telegram_id(telegram_id)
        memberships = user.memberships.all().select_related('channel')
        serializer = UserGroupMembershipSerializer(memberships, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='tgid/(?P<telegram_id>[0-9]+)/messages')
    def messages_by_telegram_id(self, request, telegram_id=None):
        """Get all messages sent by user by Telegram ID."""
        user = self._get_user_by_telegram_id(telegram_id)
        messages = ArchivedMessage.objects.from_active_accounts().filter(
            sender_id=user.telegram_id
        ).select_related('channel').order_by('-telegram_date')

        page = self.paginate_queryset(messages)
        if page is not None:
            serializer = ArchivedMessageListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = ArchivedMessageListSerializer(messages, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'], url_path='tags/add')
    def add_tags(self, request, pk=None):
        """Add tags to a user. Expects {"tags": [1, 2]} (tag IDs)."""
        user = self.get_object()
        tag_ids = request.data.get('tags', [])
        tags = Tag.objects.filter(id__in=tag_ids)
        user.tags.add(*tags)
        return Response(TagCompactSerializer(user.tags.all(), many=True).data)

    @action(detail=True, methods=['post'], url_path='tags/remove')
    def remove_tags(self, request, pk=None):
        """Remove tags from a user. Expects {"tags": [1, 2]} (tag IDs)."""
        user = self.get_object()
        tag_ids = request.data.get('tags', [])
        user.tags.remove(*tag_ids)
        return Response(TagCompactSerializer(user.tags.all(), many=True).data)