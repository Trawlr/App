from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from downloads.models import ArchivedMessage
from audit.models import MessageEntity
from api.serializers.messages import (
    ArchivedMessageListSerializer,
    ArchivedMessageDetailSerializer,
    MessageEntitySerializer
)
from api.filters import ArchivedMessageFilter
from django_filters.rest_framework import DjangoFilterBackend
from search.filters import search_messages


class ArchivedMessageViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Provides basic filtering/searching for messages archived in Trawlr
    For advanced search, use the /messages/search API endpoint
    """
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = ArchivedMessageFilter
    ordering = ['-telegram_date']

    def get_queryset(self):
        return ArchivedMessage.objects.from_active_accounts().select_related('channel', 'downloaded_file')

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ArchivedMessageDetailSerializer
        return ArchivedMessageListSerializer

    @action(detail=True, methods=['get'])
    def entities(self, request, pk=None):
        """Get entities (URLs, mentions, hashtags) for this message."""
        message = self.get_object()
        entities = message.entities.all()
        serializer = MessageEntitySerializer(entities, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def search(self, request):
        """
        Advanced search in messages using the same query syntax as the web UI.

        Query parameter: q

        Supported syntax:
          text:keyword          - Full-text search in message text
          text:"exact phrase"   - Exact match (case-insensitive)
          url:example.com       - URL search
          mention:@username     - Mention search
          hashtag:#tag          - Hashtag search
          email:*@domain.com    - Email search
          channel:channelname   - Filter by channel title/username/id
          sender:username       - Filter by sender name/username/id
          created<=7d           - Messages from last 7 days
          created>=2026-01-01   - Messages since date
          has_media:true        - Messages with media
          media_type:photo      - Filter by media type

        Operators: AND (implicit), OR, NOT, - (negation), parentheses for grouping

        Examples:
          ?q=text:test AND channel:test123
          ?q=sender:bigdawg OR sender:littledawg
          ?q=-hashtag:telegram created<=7d
        """
        base_qs = self.get_queryset()
        query = request.query_params.get('q', '')

        if query:
            qs = search_messages(query, base_qs)
        else:
            qs = base_qs

        # Apply ordering
        qs = self.filter_queryset(qs)

        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(qs, many=True)
        return Response(serializer.data)
