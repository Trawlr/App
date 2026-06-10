"""
Search views for Trawlr.
"""

import csv
import logging

from django.contrib.auth.decorators import login_required
from silk.profiling.profiler import silk_profile
from django.core.paginator import Paginator
from django.db.models import Count, F, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render

from audit.models import MessageEntity, TelegramChannel, TelegramUser
from downloads.models import ArchivedMessage, DownloadedFile, DownloadTask

from .filters import extract_archived_mode, search_messages, validate_query
from .parser import parse_query

logger = logging.getLogger('trawlr.search')


@login_required
@silk_profile(name='search.search_view')
def search_view(request):
    """Main search page."""

    query = request.GET.get('q', '').strip()
    page_number = request.GET.get('page', 1)
    per_page = request.GET.get('per_page', 50)
    sort = request.GET.get('sort', 'newest')

    # Determine archived mode from query
    archived_mode = extract_archived_mode(query)

    # Get user's accessible channels
    user_channels = TelegramChannel.objects.from_active_accounts()
    if archived_mode == 'archived':
        user_channels = user_channels.filter(active=False)
    elif archived_mode == 'all':
        pass  # No active filter — include both
    else:
        user_channels = user_channels.filter(active=True)

    # Base queryset limited to user's channels
    base_qs = ArchivedMessage.objects.filter(
        channel__in=user_channels
    ).select_related('channel', 'downloaded_file')

    results = None
    error = None
    warnings = validate_query(query) if query else []

    if query:
        try:
            # Parse and execute search; rank annotation is built from the
            # same AST as the filters (no re-parsing).
            results = search_messages(query, base_qs, with_rank=(sort == 'relevance'))

            # Apply sorting
            if sort == 'oldest':
                results = results.order_by('telegram_date')
            elif sort == 'relevance' and 'rank' in results.query.annotations:
                results = results.order_by('-rank', '-telegram_date')
            else:
                results = results.order_by('-telegram_date')

        except Exception as e:
            logger.exception(f"Search error: {e}")
            error = f"Invalid search query: {str(e)}"
            results = base_qs.none()
    else:
        results = base_qs.order_by('-telegram_date')

    # Pagination
    try:
        per_page = max(10, min(500, int(per_page)))
    except (ValueError, TypeError):
        per_page = 50

    paginator = Paginator(results, per_page)
    page_obj = paginator.get_page(page_number)

    # Get known users for linking senders
    sender_ids = set(
        m.sender_id for m in page_obj if m.sender_id
    )
    known_users = {}
    if sender_ids:
        known_users = dict(
            TelegramUser.objects.filter(
                telegram_id__in=sender_ids
            ).values_list('telegram_id', 'pk')
        )

    # Get channels for filter dropdown
    channels = user_channels.order_by('title')

    # Get pending download message IDs for status badges
    pending_message_ids = set(
        DownloadTask.objects.filter(
            channel__in=user_channels,
            status__in=['pending', 'downloading']
        ).values_list('message_id', flat=True)
    )

    context = {
        'query': query,
        'results': page_obj,
        'page_obj': page_obj,
        'error': error,
        'sort': sort,
        'per_page': per_page,
        'warnings': warnings,
        'channels': channels,
        'known_users': known_users,
        'pending_message_ids': pending_message_ids,
        'search_help': get_search_help(),
    }

    return render(request, 'search/search.html', context)


@login_required
@silk_profile(name='search.autocomplete')
def search_autocomplete(request):
    """Autocomplete suggestions for search fields."""

    field = request.GET.get('field', '')
    term = request.GET.get('term', '').strip()

    if not term or len(term) < 2:
        return JsonResponse({'suggestions': []})

    user_channels = TelegramChannel.objects.filter(
        active=True
    )

    suggestions = []

    if field == 'channel':
        channels = user_channels.filter(
            title__icontains=term
        ).values_list('title', flat=True)[:10]
        suggestions = list(channels)

    elif field == 'sender':
        senders = ArchivedMessage.objects.filter(
            channel__in=user_channels
        ).filter(
            sender_username__icontains=term
        ).values_list('sender_username', flat=True).distinct()[:10]
        suggestions = [f"@{s}" for s in senders if s]

    elif field == 'hashtag':
        hashtags = MessageEntity.objects.filter(
            message__channel__in=user_channels,
            entity__entity_type='hashtag',
            entity__text__icontains=term,
        ).values_list('entity__text', flat=True).distinct()[:10]
        suggestions = list(hashtags)

    elif field == 'mention':
        mentions = MessageEntity.objects.filter(
            message__channel__in=user_channels,
            entity__entity_type='mention',
            entity__text__icontains=term,
        ).values_list('entity__text', flat=True).distinct()[:10]
        suggestions = list(mentions)

    return JsonResponse({'suggestions': suggestions})


@login_required
@silk_profile(name='search.global_search')
def global_search(request):
    """Global search across sources, users, and files."""

    term = request.GET.get('q', '').strip()

    if not term or len(term) < 2:
        return JsonResponse({'results': []})

    results = []

    # Check if term looks like a numeric ID
    is_numeric = term.isdigit()

    # Search Sources (TelegramChannel)
    source_q = Q(title__icontains=term) | Q(username__icontains=term)
    if is_numeric:
        source_q |= Q(telegram_id=int(term))
    sources = (
        TelegramChannel.objects.from_active_accounts()
        .filter(active=True)
        .filter(source_q)[:5]
    )
    for s in sources:
        subtitle = f"@{s.username}" if s.username else s.channel_type.title()
        results.append({
            'type': 'source',
            'label': s.title,
            'subtitle': subtitle,
            'telegram_id': s.telegram_id,
            'url': f'/source/{s.pk}/',
            'icon': 'broadcast-pin',
        })

    # Search Users (TelegramUser)
    user_q = Q(username__icontains=term) | Q(first_name__icontains=term) | Q(last_name__icontains=term)
    if is_numeric:
        user_q |= Q(telegram_id=int(term))
    users = TelegramUser.objects.filter(user_q)[:5]
    for u in users:
        subtitle = f"@{u.username}" if u.username else f"ID: {u.telegram_id}"
        results.append({
            'type': 'user',
            'label': u.display_name or u.username or str(u.telegram_id),
            'subtitle': subtitle,
            'telegram_id': u.telegram_id,
            'url': f'/user/{u.pk}/',
            'icon': 'person',
        })

    # Search Files (DownloadedFile) - by filename or SHA256
    file_q = Q(original_filename__icontains=term)
    if len(term) >= 8:
        file_q |= Q(sha256_hash__istartswith=term)
    files = DownloadedFile.objects.filter(file_q).select_related('channel')[:5]
    for f in files:
        subtitle = f.channel.title if f.channel else f.file_type.title()
        results.append({
            'type': 'file',
            'label': f.original_filename or f.stored_filename,
            'subtitle': subtitle,
            'url': f'/file/{f.pk}/',
            'icon': 'file-earmark',
        })

    return JsonResponse({'results': results})


def get_search_help():
    """Return search syntax help text."""
    return {
        'fields': [
            {'name': 'text', 'desc': 'Search message text', 'example': 'text:keyword'},
            {'name': 'url', 'desc': 'Search URLs', 'example': 'url:example.com'},
            {'name': 'domain', 'desc': 'Filter by extracted domain', 'example': 'domain:t.me'},
            {'name': 'mention', 'desc': 'Search @mentions', 'example': 'mention:@username'},
            {'name': 'hashtag', 'desc': 'Search #hashtags', 'example': 'hashtag:#news'},
            {'name': 'email', 'desc': 'Search emails', 'example': 'email:*@domain.com'},
            {'name': 'phone', 'desc': 'Search phone numbers', 'example': 'phone:+1234'},
            {'name': 'channel', 'desc': 'Filter by channel (name or ID). Aliases: group, source', 'example': 'channel:news'},
            {'name': 'sender', 'desc': 'Filter by sender (name or ID). Aliases: user, username', 'example': 'sender:username'},
            {'name': 'created', 'desc': 'Date filter (s/min/h/d/w/mo/y). Alias: date', 'example': 'created>=30min'},
            {'name': 'has_media', 'desc': 'Has media. Alias: media', 'example': 'has_media:true'},
            {'name': 'media_type', 'desc': 'Media type. Alias: type', 'example': 'media_type:photo'},
            {'name': 'downloaded', 'desc': 'Media has been downloaded locally', 'example': 'downloaded:true'},
            {'name': 'deleted', 'desc': 'Chat/channel was deleted', 'example': 'deleted:true'},
            {'name': 'tag', 'desc': 'Filter by channel tag', 'example': 'tag:osint'},
            {'name': 'archived', 'desc': 'Include archived sources (true/all)', 'example': 'archived:true'},
            {'name': 'sha256', 'desc': 'Filter by downloaded file SHA256. Alias: hash', 'example': 'sha256:abc123…'},
        ],
        'operators': [
            {'name': 'AND', 'desc': 'Both conditions (implicit)', 'example': 'text:foo text:bar'},
            {'name': 'OR', 'desc': 'Either condition', 'example': 'text:foo OR text:bar'},
            {'name': 'NOT / -', 'desc': 'Exclude', 'example': '-channel:spam'},
            {'name': '"quotes"', 'desc': 'Exact match', 'example': 'sender:"r00tof"'},
        ],
        'examples': [
            'bitcoin',
            'text:crypto channel:news',
            'url:t.me created<=7d',
            'hashtag:#breaking OR hashtag:#urgent',
            'media:true type:file',
            'sender:"exactname"',
            'sender:123456789',
        ],
    }


# =============================================================================
# Aggregation Search
# =============================================================================

AGGREGATION_GROUPS = {
    'domain': {'label': 'Domain', 'icon': 'bi-globe'},
    'url': {'label': 'URL', 'icon': 'bi-link-45deg'},
    'sender': {'label': 'Sender', 'icon': 'bi-person'},
    'channel': {'label': 'Channel', 'icon': 'bi-broadcast-pin'},
    'hashtag': {'label': 'Hashtag', 'icon': 'bi-hash'},
    'mention': {'label': 'Mention', 'icon': 'bi-at'},
    'media_type': {'label': 'Media Type', 'icon': 'bi-image'},
    'email': {'label': 'Email', 'icon': 'bi-envelope'},
    'phone': {'label': 'Phone', 'icon': 'bi-phone'},
}


def _build_aggregation_qs(message_qs, group_by, sort_order):
    """
    Build an aggregation queryset from filtered messages.
    Returns a queryset of dicts with 'label' and 'count' keys.
    """
    order = 'count' if sort_order == 'asc' else '-count'

    # Entity-based aggregations use a direct JOIN through Django's reverse FK
    # (ArchivedMessage.entities) instead of a subquery.  This generates a flat
    # FROM archivedmessage INNER JOIN messageentity … WHERE channel_id IN (…)
    # which lets PostgreSQL choose the optimal join order without Semi Join
    # overhead.  The existing (message_id, entity_type) composite index on
    # MessageEntity supports this join pattern.
    entity_types = {
        'domain': 'domain',
        'hashtag': 'hashtag',
        'mention': 'mention',
        'email': 'email',
        'phone': 'phone',
    }

    if group_by in entity_types:
        # All conditions in a single .filter() so Django generates one JOIN
        # with WHERE conditions, not a separate anti-join subquery.
        return message_qs.filter(
            entities__entity__entity_type=entity_types[group_by],
            entities__entity__text__gt='',
        ).values(label=F('entities__entity__text')).annotate(
            count=Count('entities__id')
        ).order_by(order)

    if group_by == 'url':
        return message_qs.filter(
            entities__entity__entity_type__in=['url', 'text_url'],
            entities__entity__url__gt='',
        ).values(label=F('entities__entity__url')).annotate(
            count=Count('entities__id')
        ).order_by(order)

    if group_by == 'sender':
        return message_qs.exclude(
            sender_id__isnull=True
        ).values(
            'sender_id', 'sender_username', 'sender_name'
        ).annotate(
            count=Count('id')
        ).order_by(order)

    if group_by == 'channel':
        return message_qs.values(
            'channel_id', 'channel__title', 'channel__username'
        ).annotate(
            count=Count('id')
        ).order_by(order)

    if group_by == 'media_type':
        return message_qs.filter(
            has_media=True
        ).exclude(
            media_type=''
        ).values(label=F('media_type')).annotate(
            count=Count('id')
        ).order_by(order)

    raise ValueError(f"Unknown group_by: {group_by}")


def _get_result_label(row, group_by):
    """Extract a display label from an aggregation result row."""
    if group_by == 'sender':
        username = row.get('sender_username', '')
        name = row.get('sender_name', '')
        if username:
            return f"@{username}"
        return name or str(row.get('sender_id', ''))

    if group_by == 'channel':
        title = row.get('channel__title', '')
        username = row.get('channel__username', '')
        if title:
            return title
        return f"@{username}" if username else str(row.get('channel_id', ''))

    return row.get('label', '')


def _get_search_link(row, group_by):
    """Build a link to the regular search page pre-filtered to this result."""
    if group_by == 'sender':
        sender_id = row.get('sender_id')
        if sender_id:
            return f"/search/?q=sender:{sender_id}"
        return None

    if group_by == 'channel':
        channel_id = row.get('channel_id')
        if channel_id:
            return f"/search/?q=channel:{channel_id}"
        return None

    label = row.get('label', '')
    if not label:
        return None

    field_map = {
        'domain': 'domain',
        'url': 'url',
        'hashtag': 'hashtag',
        'mention': 'mention',
        'email': 'email',
        'phone': 'phone',
        'media_type': 'media_type',
    }
    field = field_map.get(group_by)
    if field:
        return f'/search/?q={field}:"{label}"'
    return None


@login_required
@silk_profile(name='search.aggregate_view')
def aggregate_view(request):
    """Aggregation search page — group and count messages by dimension."""
    DEFAULT_AGGREGATE_QUERY = 'created<24h'
    query = request.GET.get('q', '').strip()
    if 'q' not in request.GET:
        query = DEFAULT_AGGREGATE_QUERY
    group_by = request.GET.get('group_by', 'domain')
    sort = request.GET.get('sort', 'desc')
    page_number = request.GET.get('page', 1)
    per_page = request.GET.get('per_page', 50)
    min_count = request.GET.get('min_count', '').strip()
    max_count = request.GET.get('max_count', '').strip()

    if group_by not in AGGREGATION_GROUPS:
        group_by = 'domain'

    # Determine archived mode from query
    archived_mode = extract_archived_mode(query)

    # Materialize channel IDs to avoid nested subqueries
    channels_qs = TelegramChannel.objects.from_active_accounts()
    if archived_mode == 'archived':
        channels_qs = channels_qs.filter(active=False)
    elif archived_mode == 'all':
        pass
    else:
        channels_qs = channels_qs.filter(active=True)

    user_channel_ids = list(channels_qs.values_list('pk', flat=True))

    # Base queryset with materialized IDs (avoids IN (SELECT ...) nesting)
    base_qs = ArchivedMessage.objects.filter(
        channel_id__in=user_channel_ids
    )

    results = None
    has_results = False
    page_obj = None
    error = None
    total_count = 0
    warnings = validate_query(query) if query else []

    try:
        # Apply search filters
        if query:
            filtered_qs = search_messages(query, base_qs)
        else:
            filtered_qs = base_qs

        # Build aggregation
        agg_qs = _build_aggregation_qs(filtered_qs, group_by, sort)

        # Apply count thresholds
        if min_count:
            agg_qs = agg_qs.filter(count__gte=int(min_count))
        if max_count:
            agg_qs = agg_qs.filter(count__lte=int(max_count))

        # Pagination
        try:
            per_page = max(10, min(500, int(per_page)))
        except (ValueError, TypeError):
            per_page = 50

        # Materialize results into a list so Paginator uses len() instead
        # of running an expensive COUNT(*) query against the DB.
        # Aggregation rows are small (label + count), so 10K rows ≈ 200KB.
        MAX_AGGREGATE_RESULTS = 10_000
        results_list = list(agg_qs[:MAX_AGGREGATE_RESULTS])
        paginator = Paginator(results_list, per_page)
        page_obj = paginator.get_page(page_number)
        total_count = paginator.count

        # Compute max count on this page for bar width
        bar_max = max((row['count'] for row in page_obj), default=1)

        # Enrich results with display labels and search links
        enriched = []
        for row in page_obj:
            enriched.append({
                'label': _get_result_label(row, group_by),
                'count': row['count'],
                'bar_pct': round(row['count'] / bar_max * 100) if bar_max else 0,
                'search_link': _get_search_link(row, group_by),
            })

        results = enriched
        has_results = True

    except Exception as e:
        has_results = False
        logger.exception(f"Aggregation error: {e}")
        error = f"Aggregation query failed: {str(e)}"
        page_obj = None

        try:
            per_page = max(10, min(500, int(per_page)))
        except (ValueError, TypeError):
            per_page = 50

    context = {
        'query': query,
        'group_by': group_by,
        'sort': sort,
        'per_page': per_page,
        'min_count': min_count,
        'max_count': max_count,
        'results': results,
        'has_results': has_results,
        'page_obj': page_obj,
        'group_by_label': AGGREGATION_GROUPS[group_by]['label'],
        'error': error,
        'warnings': warnings,
        'total_count': total_count,
        'aggregation_groups': AGGREGATION_GROUPS,
        'search_help': get_search_help(),
    }

    return render(request, 'search/aggregate.html', context)


@login_required
@silk_profile(name='search.aggregate_export')
def aggregate_export(request):
    """Export aggregation results as CSV."""
    query = request.GET.get('q', '').strip()
    group_by = request.GET.get('group_by', 'domain')
    sort = request.GET.get('sort', 'desc')
    min_count = request.GET.get('min_count', '').strip()
    max_count = request.GET.get('max_count', '').strip()

    if group_by not in AGGREGATION_GROUPS:
        group_by = 'domain'

    # Determine archived mode from query
    archived_mode = extract_archived_mode(query)

    channels_qs = TelegramChannel.objects.from_active_accounts()
    if archived_mode == 'archived':
        channels_qs = channels_qs.filter(active=False)
    elif archived_mode == 'all':
        pass
    else:
        channels_qs = channels_qs.filter(active=True)

    user_channel_ids = list(channels_qs.values_list('pk', flat=True))
    base_qs = ArchivedMessage.objects.filter(channel_id__in=user_channel_ids)

    if query:
        filtered_qs = search_messages(query, base_qs)
    else:
        filtered_qs = base_qs

    agg_qs = _build_aggregation_qs(filtered_qs, group_by, sort)

    if min_count:
        agg_qs = agg_qs.filter(count__gte=int(min_count))
    if max_count:
        agg_qs = agg_qs.filter(count__lte=int(max_count))

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="aggregate_{group_by}.csv"'

    writer = csv.writer(response)
    label_name = AGGREGATION_GROUPS[group_by]['label']
    writer.writerow([label_name, 'Count'])

    for row in agg_qs[:10000]:
        writer.writerow([_get_result_label(row, group_by), row['count']])

    return response
