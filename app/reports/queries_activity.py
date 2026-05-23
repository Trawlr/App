"""
Activity Map queries — temporal pattern-of-life heatmaps.

Three scopes:
  - source: one channel
  - user:   one TelegramUser (across their channels, optionally filtered by channel_id)
  - aggregate: all active sources, optionally filtered by tag/channel_type

Two layouts:
  - calendar: daily buckets (YYYY-MM-DD -> count)
  - hourdow:  hour-of-day x ISO weekday (0..23 x 1..7) -> count

Five metrics:
  - posts:   ArchivedMessage rows bucketed by telegram_date (when posted)
  - edits:   ArchivedMessage rows where edited_date is set, bucketed by edited_date
  - deletes: ArchivedMessage rows where deleted_at is set, bucketed by deleted_at
  - joins:   UserGroupMembership rows bucketed by first_seen (proxy for join time)
  - media:   ArchivedMessage rows where has_media=True, bucketed by telegram_date

All time bucketing uses the user-supplied IANA timezone, executed in Postgres
via Django's ExtractHour/ExtractIsoWeekDay/TruncDate tzinfo= argument.
"""

from datetime import datetime, date as date_cls, time as time_cls, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import math

from django.db.models import Count
from django.db.models.functions import (
    ExtractHour,
    ExtractIsoWeekDay,
    TruncDate,
)
from django.utils import timezone

from audit.models import (
    TelegramChannel,
    TelegramUser,
    UserGroupMembership,
)
from downloads.models import ArchivedMessage


ACTIVITY_DAYS_PRESETS = {'1', '30', '90', '180', '365'}
DAYS_CUSTOM = 'custom'
DEFAULT_ACTIVITY_DAYS = 30
MAX_CUSTOM_DAYS = 365 * 2  # cap custom range to avoid pathological queries

LAYOUT_CALENDAR = 'calendar'
LAYOUT_HOURDOW = 'hourdow'
ALLOWED_LAYOUTS = {LAYOUT_CALENDAR, LAYOUT_HOURDOW}

# Metrics
METRIC_POSTS = 'posts'
METRIC_EDITS = 'edits'
METRIC_DELETES = 'deletes'
METRIC_JOINS = 'joins'
METRIC_MEDIA = 'media'
ALLOWED_METRICS = {METRIC_POSTS, METRIC_EDITS, METRIC_DELETES, METRIC_JOINS, METRIC_MEDIA}

# Backwards-compat alias for any callers still passing 'messages'
_METRIC_ALIASES = {'messages': METRIC_POSTS}

# Scopes
SCOPE_SOURCE = 'source'
SCOPE_USER = 'user'
SCOPE_AGGREGATE = 'aggregate'
ALLOWED_SCOPES = {SCOPE_SOURCE, SCOPE_USER, SCOPE_AGGREGATE}

# Drill-down only makes sense for metrics that map onto the message list;
# joins/deletes have no matching message-list view that filters by date.
_METRICS_WITH_DRILLDOWN = {METRIC_POSTS, METRIC_EDITS, METRIC_MEDIA}


def parse_activity_params(request):
    """Parse and validate Activity Map GET params. Returns a dict."""
    scope = request.GET.get('scope', SCOPE_AGGREGATE)
    if scope not in ALLOWED_SCOPES:
        scope = SCOPE_AGGREGATE

    scope_id = request.GET.get('id')
    try:
        scope_id = int(scope_id) if scope_id else None
    except (TypeError, ValueError):
        scope_id = None

    days_raw = request.GET.get('days', str(DEFAULT_ACTIVITY_DAYS))

    tz_name = request.GET.get('tz', 'UTC') or 'UTC'
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz_name = 'UTC'
        tz = ZoneInfo('UTC')

    layout = request.GET.get('layout', LAYOUT_CALENDAR)
    if layout not in ALLOWED_LAYOUTS:
        layout = LAYOUT_CALENDAR

    metric = request.GET.get('metric', METRIC_POSTS)
    metric = _METRIC_ALIASES.get(metric, metric)
    if metric not in ALLOWED_METRICS:
        metric = METRIC_POSTS

    compare = request.GET.get('compare', '').lower() in ('1', 'true', 'yes', 'on')

    channel_ids = _parse_csv_ints(request.GET.get('channel_id', ''))
    tag_ids = _parse_csv_ints(request.GET.get('tags', ''))
    channel_types = [t for t in request.GET.get('channel_type', '').split(',') if t]

    # Resolve time window — either a preset (1/30/90/180/365 days back from now)
    # or a custom calendar range (start + end as YYYY-MM-DD in the user's tz).
    days_label, days_int, start, end = _resolve_window(request, days_raw, tz)

    return {
        'scope': scope,
        'scope_id': scope_id,
        'days': days_int,
        'days_label': days_label,
        'tz_name': tz_name,
        'tz': tz,
        'layout': layout,
        'metric': metric,
        'compare': compare,
        'channel_ids': channel_ids,
        'tag_ids': tag_ids,
        'channel_types': channel_types,
        'start': start,
        'end': end,
    }


def _resolve_window(request, days_raw, tz):
    """
    Returns (days_label, days_int, start, end). days_label is 'custom' or one
    of ACTIVITY_DAYS_PRESETS so the frontend knows which UI to render. days_int
    is the integer span used by the compare-period offset.
    """
    if days_raw == DAYS_CUSTOM:
        parsed = _parse_custom_range(request, tz)
        if parsed is not None:
            start, end, span = parsed
            return DAYS_CUSTOM, span, start, end
        # Fall through to default if custom dates are missing or invalid.

    days = days_raw if days_raw in ACTIVITY_DAYS_PRESETS else str(DEFAULT_ACTIVITY_DAYS)
    days_int = int(days)
    now = timezone.now()
    return days, days_int, now - timedelta(days=days_int), now


def _parse_custom_range(request, tz):
    """Parse start/end (YYYY-MM-DD) as midnight in the user's tz. Returns None if invalid."""
    start_str = request.GET.get('start', '')
    end_str = request.GET.get('end', '')
    try:
        start_d = date_cls.fromisoformat(start_str)
        end_d = date_cls.fromisoformat(end_str)
    except ValueError:
        return None
    if start_d > end_d:
        return None
    span_days = (end_d - start_d).days + 1
    if span_days > MAX_CUSTOM_DAYS:
        return None
    # Inclusive of the entire end day → end_ts is start of (end_d + 1).
    start_ts = datetime.combine(start_d, time_cls.min, tzinfo=tz)
    end_ts = datetime.combine(end_d + timedelta(days=1), time_cls.min, tzinfo=tz)
    return start_ts, end_ts, span_days


def _parse_csv_ints(raw):
    if not raw:
        return []
    out = []
    for piece in raw.split(','):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.append(int(piece))
        except ValueError:
            continue
    return out


def metric_supports_drilldown(metric):
    return metric in _METRICS_WITH_DRILLDOWN


# =============================================================================
# Per-metric query builders
# =============================================================================

def _aggregate_channel_filter(params):
    """Subquery of channel ids matching the aggregate filters (tags/types)."""
    qs = TelegramChannel.objects.filter(active=True)
    if params['tag_ids']:
        qs = qs.filter(tags__in=params['tag_ids'])
    if params['channel_types']:
        qs = qs.filter(channel_type__in=params['channel_types'])
    return qs.values('id')


def _scope_archived_messages(params, time_field, start, end):
    """ArchivedMessage QS scoped per request, time-windowed on the given field."""
    qs = ArchivedMessage.objects.from_active_accounts().filter(
        **{f'{time_field}__gte': start, f'{time_field}__lt': end}
    )
    if params['scope'] == SCOPE_SOURCE and params['scope_id']:
        qs = qs.filter(channel_id=params['scope_id'])
    elif params['scope'] == SCOPE_USER and params['scope_id']:
        try:
            tg_user = TelegramUser.objects.only('telegram_id').get(pk=params['scope_id'])
        except TelegramUser.DoesNotExist:
            return qs.none()
        qs = qs.filter(sender_id=tg_user.telegram_id)
        if params['channel_ids']:
            qs = qs.filter(channel_id__in=params['channel_ids'])
    elif params['scope'] == SCOPE_AGGREGATE and (params['tag_ids'] or params['channel_types']):
        qs = qs.filter(channel_id__in=_aggregate_channel_filter(params))
    return qs


def _scope_memberships(params, start, end):
    """UserGroupMembership QS scoped per request, time-windowed on first_seen."""
    qs = UserGroupMembership.objects.filter(
        first_seen__gte=start,
        first_seen__lt=end,
        channel__account__is_active=True,
    )
    if params['scope'] == SCOPE_SOURCE and params['scope_id']:
        qs = qs.filter(channel_id=params['scope_id'])
    elif params['scope'] == SCOPE_USER and params['scope_id']:
        qs = qs.filter(user_id=params['scope_id'])
        if params['channel_ids']:
            qs = qs.filter(channel_id__in=params['channel_ids'])
    elif params['scope'] == SCOPE_AGGREGATE and (params['tag_ids'] or params['channel_types']):
        qs = qs.filter(channel_id__in=_aggregate_channel_filter(params))
    return qs




# =============================================================================
# Bucketing
# =============================================================================

def _db_calendar_buckets(qs, time_field, params):
    rows = (
        qs.annotate(day=TruncDate(time_field, tzinfo=params['tz']))
        .values('day')
        .annotate(c=Count('id'))
        .order_by('day')
    )
    return [{'date': r['day'].isoformat(), 'count': r['c']} for r in rows if r['day']]


def _db_hour_dow_buckets(qs, time_field, params):
    rows = (
        qs.annotate(
            hour=ExtractHour(time_field, tzinfo=params['tz']),
            dow=ExtractIsoWeekDay(time_field, tzinfo=params['tz']),
        )
        .values('hour', 'dow')
        .annotate(c=Count('id'))
    )
    return [
        {'hour': r['hour'], 'dow': r['dow'], 'count': r['c']}
        for r in rows
        if r['hour'] is not None and r['dow'] is not None
    ]


def get_calendar_buckets(params, start, end):
    metric = params['metric']
    if metric == METRIC_DELETES:
        qs = _scope_archived_messages(params, 'deleted_at', start, end).filter(deleted_at__isnull=False)
        return _db_calendar_buckets(qs, 'deleted_at', params)
    if metric == METRIC_JOINS:
        return _db_calendar_buckets(_scope_memberships(params, start, end), 'first_seen', params)
    if metric == METRIC_EDITS:
        qs = _scope_archived_messages(params, 'edited_date', start, end).filter(edited_date__isnull=False)
        return _db_calendar_buckets(qs, 'edited_date', params)
    if metric == METRIC_MEDIA:
        qs = _scope_archived_messages(params, 'telegram_date', start, end).filter(has_media=True)
        return _db_calendar_buckets(qs, 'telegram_date', params)
    qs = _scope_archived_messages(params, 'telegram_date', start, end)
    return _db_calendar_buckets(qs, 'telegram_date', params)


def get_hour_dow_buckets(params, start, end):
    metric = params['metric']
    if metric == METRIC_DELETES:
        qs = _scope_archived_messages(params, 'deleted_at', start, end).filter(deleted_at__isnull=False)
        return _db_hour_dow_buckets(qs, 'deleted_at', params)
    if metric == METRIC_JOINS:
        return _db_hour_dow_buckets(_scope_memberships(params, start, end), 'first_seen', params)
    if metric == METRIC_EDITS:
        qs = _scope_archived_messages(params, 'edited_date', start, end).filter(edited_date__isnull=False)
        return _db_hour_dow_buckets(qs, 'edited_date', params)
    if metric == METRIC_MEDIA:
        qs = _scope_archived_messages(params, 'telegram_date', start, end).filter(has_media=True)
        return _db_hour_dow_buckets(qs, 'telegram_date', params)
    qs = _scope_archived_messages(params, 'telegram_date', start, end)
    return _db_hour_dow_buckets(qs, 'telegram_date', params)


# =============================================================================
# Anomaly detection (z-score over the bucket distribution)
# =============================================================================

def compute_calendar_anomalies(buckets, z_threshold=2.0):
    counts = [b['count'] for b in buckets]
    if len(counts) < 7:
        return []
    mean = sum(counts) / len(counts)
    variance = sum((c - mean) ** 2 for c in counts) / len(counts)
    std = math.sqrt(variance)
    if std == 0:
        return []
    return [b['date'] for b in buckets if (b['count'] - mean) / std > z_threshold]


def compute_hourdow_anomalies(buckets, z_threshold=2.0):
    if not buckets:
        return []
    by_hour = {}
    for b in buckets:
        by_hour.setdefault(b['hour'], []).append(b['count'])

    flagged = []
    for b in buckets:
        peers = by_hour[b['hour']]
        if len(peers) < 3:
            continue
        mean = sum(peers) / len(peers)
        variance = sum((c - mean) ** 2 for c in peers) / len(peers)
        std = math.sqrt(variance)
        if std == 0:
            continue
        if (b['count'] - mean) / std > z_threshold:
            flagged.append({'hour': b['hour'], 'dow': b['dow']})
    return flagged


# =============================================================================
# Sidebar / chip data + footer summary
# =============================================================================

def get_top_channels_for_user(user_pk, start, end, limit=15):
    """Top channels this user posts in within the given window (for user-page sidebar)."""
    try:
        tg_user = TelegramUser.objects.only('telegram_id').get(pk=user_pk)
    except TelegramUser.DoesNotExist:
        return []

    rows = (
        ArchivedMessage.objects.from_active_accounts()
        .filter(
            sender_id=tg_user.telegram_id,
            telegram_date__gte=start,
            telegram_date__lt=end,
        )
        .values('channel_id', 'channel__title', 'channel__username')
        .annotate(count=Count('id'))
        .order_by('-count')[:limit]
    )
    return [
        {
            'channel_id': r['channel_id'],
            'title': r['channel__title'] or r['channel__username'] or f'#{r["channel_id"]}',
            'count': r['count'],
        }
        for r in rows
    ]


def get_aggregate_filter_options():
    """Filter chip data for the aggregate map page."""
    from audit.models import Tag

    tags = list(
        Tag.objects.filter(channels__active=True)
        .distinct()
        .values('id', 'name', 'colour')
        .order_by('name')[:30]
    )
    channel_types = list(
        TelegramChannel.objects.filter(active=True)
        .values_list('channel_type', flat=True)
        .distinct()
        .order_by('channel_type')
    )
    return {'tags': tags, 'channel_types': [ct for ct in channel_types if ct]}


def compute_summary(params, calendar_buckets):
    """Footer totals shown beneath the heatmap."""
    total = sum(b['count'] for b in calendar_buckets)
    active_buckets = sum(1 for b in calendar_buckets if b['count'] > 0)
    return {
        'total': total,
        'active_buckets': active_buckets,
    }
