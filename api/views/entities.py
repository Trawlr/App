from django.db.models import Count
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from audit.models import GlobalEntity
from downloads.models import ArchivedMessage
from api.filters import GlobalEntityFilter
from api.serializers.entities import (
    GlobalEntityListSerializer,
    GlobalEntityDetailSerializer,
)
from api.serializers.messages import ArchivedMessageListSerializer


class GlobalEntityViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Deduplicated message entity identities (URLs, domains, mentions, hashtags,
    emails, phones, custom emoji, formatting spans, ...). Each row is one unique
    entity; the same URL or hashtag shared across many messages is a single
    GlobalEntity with occurrence_count > 1.

    Useful for "top domains shared", "every message that linked to X", etc.

    Default ordering: -occurrence_count (most-shared first).
    Override with ?ordering=first_seen_at | last_seen_at | occurrence_count
    (prefix with - for descending).
    """
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_class = GlobalEntityFilter
    ordering_fields = ['occurrence_count', 'first_seen_at', 'last_seen_at']
    ordering = ['-occurrence_count']

    def get_queryset(self):
        return GlobalEntity.objects.annotate(
            occurrence_count=Count('occurrences')
        )

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return GlobalEntityDetailSerializer
        return GlobalEntityListSerializer

    @action(detail=True, methods=['get'])
    def messages(self, request, pk=None):
        """Messages where this entity appears (active sources only)."""
        entity = self.get_object()
        messages = ArchivedMessage.objects.from_active_accounts().filter(
            entities__entity_id=entity.pk
        ).select_related('channel', 'downloaded_file').distinct().order_by('-telegram_date')

        page = self.paginate_queryset(messages)
        if page is not None:
            serializer = ArchivedMessageListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = ArchivedMessageListSerializer(messages, many=True)
        return Response(serializer.data)
