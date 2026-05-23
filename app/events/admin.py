"""
Django admin configuration for the events app.
"""

import uuid

from django.contrib import admin
from django.db.models import BigIntegerField, OuterRef, Q, Subquery
from django.db.models.expressions import RawSQL
from django.urls import reverse
from django.utils.html import format_html

from audit.models import TelegramChannel
from .models import RawEvents


@admin.register(RawEvents)
class RawEventsAdmin(admin.ModelAdmin):
    """Admin interface for RawEvents model."""

    list_display = [
        'event_id',
        'event_type',
        'account',
        'channel_name',
        'message_id',
        'event_timestamp',
        'received_at',
    ]

    def get_queryset(self, request):
        """Annotate queryset with channel title and pk from TelegramChannel.

        Handles chat_id normalization: strips -100 prefix if present to match
        TelegramChannel.telegram_id format.
        """
        qs = super().get_queryset(request)

        # Normalize chat_id using PostgreSQL string functions:
        # If chat_id starts with '-100', extract everything after (the raw channel ID)
        # Otherwise use the chat_id as-is
        normalized_id_sql = RawSQL(
            """
            CASE
                WHEN chat_id::text LIKE '-100%%'
                THEN substring(chat_id::text from 5)::bigint
                ELSE chat_id
            END
            """,
            [],
            output_field=BigIntegerField()
        )

        # Look up channel title and pk by the normalized ID
        channel_subquery = TelegramChannel.objects.filter(
            telegram_id=OuterRef('normalized_id')
        )

        return qs.annotate(
            normalized_id=normalized_id_sql,
            channel_title=Subquery(channel_subquery.values('title')[:1]),
            channel_pk=Subquery(channel_subquery.values('pk')[:1]),
        )

    @admin.display(description='Channel')
    def channel_name(self, obj):
        """Display channel title as clickable link if available, otherwise show chat_id."""
        if hasattr(obj, 'channel_title') and obj.channel_title and hasattr(obj, 'channel_pk') and obj.channel_pk:
            url = reverse('admin:audit_telegramchannel_change', args=[obj.channel_pk])
            return format_html('<a href="{}">{}</a>', url, obj.channel_title)
        return str(obj.chat_id)

    list_filter = [
        'event_type',
        'account',
        'received_at',
    ]
    # search_fields lists `event_id` purely so Django admin renders the search
    # box on the changelist — our get_search_results() override handles the
    # actual filtering for every term shape (UUID / numeric / channel title)
    # and never calls super(), so the default ILIKE-on-cast plan that times out
    # is bypassed entirely.
    search_fields = ['event_id']
    search_help_text = (
        'Event UUID, telegram chat_id or message_id (numeric, with or '
        'without -100 prefix), or part of a channel title.'
    )

    def get_search_results(self, request, queryset, search_term):
        """
        Index-friendly search.

        * UUID-shaped term: exact equality on event_id (primary key).
        * Numeric term: exact equality on chat_id/message_id (uses btree
          indexes). Also matches the -100-prefixed channel form when the
          term is positive.
        * Non-numeric term: ILIKE on the small audit_telegramchannel.title,
          then filter raw events by the resolved telegram_ids. No global
          ILIKE on the events table.
        """
        use_distinct = False
        if not search_term:
            return queryset, use_distinct

        term = search_term.strip()

        # UUID term -> primary-key lookup. Accept canonical "8-4-4-4-12" form
        # and bare 32-char hex; uuid.UUID() handles both.
        try:
            event_uuid = uuid.UUID(term)
        except (ValueError, AttributeError):
            event_uuid = None
        if event_uuid is not None:
            return queryset.filter(event_id=event_uuid), use_distinct

        # Numeric term -> indexed equality
        digits_only = term.lstrip('-')
        if digits_only.isdigit():
            try:
                n = int(term)
            except (ValueError, OverflowError):
                n = None
            if n is not None:
                id_q = Q(chat_id=n) | Q(message_id=n)
                if n > 0:
                    # Channels are often referenced as -100<id> in Telegram payloads
                    try:
                        id_q |= Q(chat_id=int(f'-100{n}'))
                    except (ValueError, OverflowError):
                        pass
                return queryset.filter(id_q), use_distinct

        # Non-numeric -> resolve via channel title (small table)
        matching_channel_ids = list(
            TelegramChannel.objects.filter(
                title__icontains=term
            ).values_list('telegram_id', flat=True)
        )
        if not matching_channel_ids:
            return queryset.none(), use_distinct

        channel_q = Q()
        for tid in matching_channel_ids:
            channel_q |= Q(chat_id=tid)
            try:
                channel_q |= Q(chat_id=int(f'-100{tid}'))
            except (ValueError, OverflowError):
                pass
        return queryset.filter(channel_q), True
    readonly_fields = [
        'event_id',
        'event_type',
        'account',
        'chat_id',
        'message_id',
        'raw_json',
        'event_timestamp',
        'received_at',
    ]
    date_hierarchy = 'received_at'
    ordering = ['-received_at']

    def has_add_permission(self, request):
        """Disable manual creation - events are created by the processor."""
        return False

    def has_change_permission(self, request, obj=None):
        """Disable editing - raw events should be immutable."""
        return False
