"""
Efficient query functions for reports.
All queries use Django ORM optimizations: TruncDate, annotate, values, select_related.
"""

from datetime import timedelta
from django.db.models import Count, Sum, Avg, Q, F, Case, When, IntegerField, Max
from django.db.models.functions import TruncDate, Cast
from django.utils import timezone

from audit.models import (
    TelegramChannel, TelegramUser, UserGroupMembership,
    MessageEntity, ForwardSource, ExclusionRule, TelegramReport,
    UserNote, ForumTopic, ChannelConfig,
)
from downloads.models import ArchivedMessage, DownloadedFile


# =============================================================================
# Date Range Helpers
# =============================================================================

DATE_PRESETS = {
    '1': 1,
    '7': 7,
    '14': 14,
    '30': 30,
    '60': 60,
    '90': 90,
    '365': 365,
}

DEFAULT_DAYS = 7


def parse_date_range(request):
    """
    Parse date range from request GET params.
    Returns (start_date, end_date, days_label).
    """
    days = request.GET.get('days')
    start = request.GET.get('start')
    end = request.GET.get('end')

    now = timezone.now()

    if start and end:
        # Custom date range
        from datetime import datetime
        try:
            start_date = timezone.make_aware(datetime.strptime(start, '%Y-%m-%d'))
            end_date = timezone.make_aware(datetime.strptime(end, '%Y-%m-%d').replace(hour=23, minute=59, second=59))
            days_label = 'custom'
            return start_date, end_date, days_label
        except ValueError:
            pass

    # Preset days
    days_int = DATE_PRESETS.get(days, DEFAULT_DAYS)
    start_date = now - timedelta(days=days_int)
    end_date = now
    days_label = str(days_int)

    return start_date, end_date, days_label


# =============================================================================
# Content Analytics Queries
# =============================================================================

def get_message_volume(start_date, end_date):
    """Get message count per day for date range."""
    return ArchivedMessage.objects.from_active_accounts().filter(
        archived_at__gte=start_date,
        archived_at__lte=end_date
    ).annotate(
        date=TruncDate('archived_at')
    ).values('date').annotate(
        count=Count('id')
    ).order_by('date')


def get_message_volume_by_source_per_day(start_date, end_date):
    """Per-source, per-day message volume (long format)."""
    return ArchivedMessage.objects.from_active_accounts().filter(
        archived_at__gte=start_date,
        archived_at__lte=end_date,
    ).annotate(
        date=TruncDate('archived_at'),
    ).values(
        'channel_id', 'channel__title', 'date',
    ).annotate(
        count=Count('id'),
    ).order_by('channel__title', 'channel_id', 'date')


def get_media_distribution(start_date, end_date):
    """Get media type breakdown."""
    return ArchivedMessage.objects.from_active_accounts().filter(
        archived_at__gte=start_date,
        archived_at__lte=end_date,
        has_media=True
    ).values('media_type').annotate(
        count=Count('id')
    ).order_by('-count')


def get_top_entities(start_date, end_date, entity_type, limit=15):
    """Get top entities (urls, hashtags, mentions) by count."""
    return MessageEntity.objects.filter(
        message__channel__account__is_active=True,
        message__archived_at__gte=start_date,
        message__archived_at__lte=end_date,
        entity__entity_type=entity_type,
    ).values(text=F('entity__text')).annotate(
        count=Count('id')
    ).order_by('-count')[:limit]


def get_top_urls(start_date, end_date, limit=15):
    """Get top URLs including text_url type."""
    return MessageEntity.objects.filter(
        message__channel__account__is_active=True,
        message__archived_at__gte=start_date,
        message__archived_at__lte=end_date,
        entity__entity_type__in=['url', 'text_url'],
    ).exclude(
        entity__url=''
    ).values(url=F('entity__url')).annotate(
        count=Count('id')
    ).order_by('-count')[:limit]


def get_top_forward_sources(start_date, end_date, limit=15):
    """Get top forward sources."""
    return ForwardSource.objects.filter(
        message__channel__account__is_active=True,
        message__archived_at__gte=start_date,
        message__archived_at__lte=end_date
    ).exclude(
        source_title=''
    ).values('source_title', 'source_type', 'source_username').annotate(
        count=Count('id')
    ).order_by('-count')[:limit]


def get_engagement_stats(start_date, end_date):
    """Get aggregate engagement metrics."""
    return ArchivedMessage.objects.from_active_accounts().filter(
        archived_at__gte=start_date,
        archived_at__lte=end_date
    ).aggregate(
        total_messages=Count('id'),
        total_views=Sum('views'),
        total_forwards=Sum('forwards'),
        with_media=Count('id', filter=Q(has_media=True)),
        edited_count=Count('id', filter=Q(edited_date__isnull=False)),
        pinned_count=Count('id', filter=Q(is_pinned=True)),
        deleted_count=Count('id', filter=Q(is_deleted=True)),
    )


def get_content_summary(start_date, end_date):
    """Get overall content summary for export."""
    stats = get_engagement_stats(start_date, end_date)
    media = list(get_media_distribution(start_date, end_date))
    return {
        'period': {
            'start': start_date.isoformat(),
            'end': end_date.isoformat(),
        },
        'messages': stats,
        'media_breakdown': media,
    }


# =============================================================================
# User Intelligence Queries
# =============================================================================

def get_user_activity_stats(start_date, end_date):
    """Get user activity aggregates."""
    return TelegramUser.objects.aggregate(
        total_users=Count('id'),
        new_users=Count('id', filter=Q(first_seen__gte=start_date)),
        active_users=Count('id', filter=Q(last_seen__gte=start_date)),
        flagged_users=Count('id', filter=Q(is_flagged=True)),
        premium_users=Count('id', filter=Q(is_premium=True)),
        verified_users=Count('id', filter=Q(is_verified=True)),
        bot_count=Count('id', filter=Q(is_bot=True)),
        scam_count=Count('id', filter=Q(is_scam=True)),
        fake_count=Count('id', filter=Q(is_fake=True)),
        restricted_count=Count('id', filter=Q(is_restricted=True)),
    )


def get_new_users_by_day(start_date, end_date):
    """Get new users tracked per day."""
    return TelegramUser.objects.filter(
        first_seen__gte=start_date,
        first_seen__lte=end_date
    ).annotate(
        date=TruncDate('first_seen')
    ).values('date').annotate(
        count=Count('id')
    ).order_by('date')


def get_cross_channel_users(limit=20):
    """Get users present in multiple active channels."""
    top_user_ids = (
        UserGroupMembership.objects.filter(active=True)
        .values('user_id')
        .annotate(channel_count=Count('id'))
        .filter(channel_count__gt=1)
        .order_by('-channel_count')
        .values_list('user_id', 'channel_count')[:limit]
    )
    top_user_ids = list(top_user_ids)
    if not top_user_ids:
        return TelegramUser.objects.none()

    counts_by_id = {uid: cnt for uid, cnt in top_user_ids}
    users = TelegramUser.objects.filter(
        id__in=counts_by_id.keys()
    ).only('id', 'telegram_id', 'username', 'first_name', 'last_name')

    for user in users:
        user.channel_count = counts_by_id[user.id]

    return sorted(users, key=lambda u: u.channel_count, reverse=True)


def get_user_churn(start_date, end_date, inactive_days=60):
    """
    Get user churn stats.
    Churn = active=False OR last_seen < (now - inactive_days)
    """
    inactive_cutoff = timezone.now() - timedelta(days=inactive_days)

    counts = UserGroupMembership.objects.aggregate(
        total_memberships=Count('id'),
        churned_count=Count('id', filter=Q(active=False) | Q(last_seen__lt=inactive_cutoff)),
    )
    total_memberships = counts['total_memberships']
    churned_count = counts['churned_count']

    churned = UserGroupMembership.objects.filter(
        Q(active=False) | Q(last_seen__lt=inactive_cutoff)
    )

    # By channel
    churn_by_channel = churned.values(
        'channel__title', 'channel_id'
    ).annotate(
        count=Count('id')
    ).order_by('-count')[:20]

    return {
        'total_memberships': total_memberships,
        'churned_count': churned_count,
        'active_count': total_memberships - churned_count,
        'churn_rate': round(churned_count / total_memberships * 100, 1) if total_memberships else 0,
        'by_channel': list(churn_by_channel),
    }


def get_flagged_users():
    """Get all flagged users."""
    return TelegramUser.objects.filter(
        is_flagged=True
    ).only(
        'id', 'telegram_id', 'username', 'first_name', 'last_name',
        'flagged_reason', 'flagged_notes', 'flagged_at'
    ).order_by('-flagged_at')


def get_suspicious_users():
    """Get scam, fake, or restricted users."""
    return TelegramUser.objects.filter(
        Q(is_scam=True) | Q(is_fake=True) | Q(is_restricted=True)
    ).only(
        'id', 'telegram_id', 'username', 'first_name', 'last_name',
        'is_scam', 'is_fake', 'is_restricted'
    ).order_by('-last_seen')[:50]


def get_top_posters(limit=20):
    """Get users with highest message counts."""
    return TelegramUser.objects.filter(
        message_count__gt=0
    ).order_by('-message_count').only(
        'id', 'telegram_id', 'username', 'first_name', 'last_name',
        'message_count', 'is_premium', 'is_verified'
    )[:limit]


# =============================================================================
# Source Analytics Queries
# =============================================================================

def get_channel_health():
    """Get channel availability status breakdown."""
    return TelegramChannel.objects.values(
        'availability_status'
    ).annotate(
        count=Count('id')
    ).order_by('-count')


def get_channel_type_breakdown():
    """Get channel type breakdown."""
    return TelegramChannel.objects.values(
        'channel_type'
    ).annotate(
        count=Count('id')
    ).order_by('-count')


def get_content_volume_by_source(start_date, end_date, limit=20):
    """Get message count by channel."""
    return ArchivedMessage.objects.from_active_accounts().filter(
        archived_at__gte=start_date,
        archived_at__lte=end_date
    ).values(
        'channel__title', 'channel_id'
    ).annotate(
        count=Count('id')
    ).order_by('-count')[:limit]


def get_download_progress():
    """Get download progress stats across all active channels."""
    configs = ChannelConfig.objects.filter(
        channel__active=True
    ).select_related('channel').annotate(
        progress_percent=Case(
            When(total_messages=0, then=0),
            default=Cast(F('downloaded_messages') * 100 / F('total_messages'), IntegerField()),
        )
    )

    total = configs.count()
    complete = configs.filter(
        total_messages__gt=0,
        downloaded_messages__gte=F('total_messages')
    ).count()

    # Calculate average progress
    totals = configs.aggregate(
        total_downloaded=Sum('downloaded_messages'),
        total_expected=Sum('total_messages')
    )
    if totals['total_expected'] and totals['total_expected'] > 0:
        avg_progress = (totals['total_downloaded'] / totals['total_expected']) * 100
    else:
        avg_progress = 0

    # Get incomplete list with calculated progress
    incomplete_list = list(
        configs.filter(
            Q(total_messages=0) | Q(downloaded_messages__lt=F('total_messages'))
        ).order_by('progress_percent').values(
            'channel__title', 'channel_id',
            'total_messages', 'downloaded_messages', 'progress_percent'
        )[:20]
    )

    # Rename progress_percent to download_progress_percent for template compatibility
    for item in incomplete_list:
        item['download_progress_percent'] = item.pop('progress_percent')

    return {
        'total_channels': total,
        'complete': complete,
        'incomplete': total - complete,
        'avg_progress': round(avg_progress, 1),
        'incomplete_list': incomplete_list,
    }


def get_forum_activity():
    """Get forum topic activity stats."""
    return ForumTopic.objects.select_related('channel').values(
        'channel__title', 'channel_id'
    ).annotate(
        topic_count=Count('id'),
        total_messages=Sum('message_count')
    ).filter(topic_count__gt=0).order_by('-total_messages')[:20]


def get_multi_account_channels():
    """Get channels seen by multiple accounts."""
    return TelegramChannel.objects.annotate(
        account_count=Count('seen_by_accounts')
    ).filter(
        account_count__gt=1
    ).order_by('-account_count').only(
        'id', 'title', 'username', 'channel_type'
    )[:20]


def get_source_stats(start_date, end_date):
    """Get overall source summary."""
    return TelegramChannel.objects.aggregate(
        total_sources=Count('id'),
        active_sources=Count('id', filter=Q(active=True)),
        new_sources=Count('id', filter=Q(joined_at__gte=start_date)),
        forum_count=Count('id', filter=Q(is_forum=True)),
        verified_count=Count('id', filter=Q(is_verified=True)),
        scam_count=Count('id', filter=Q(is_scam=True)),
        fake_count=Count('id', filter=Q(is_fake=True)),
    )


# =============================================================================
# Investigation Queries
# =============================================================================

def get_report_stats(start_date, end_date):
    """Get Telegram report submission stats."""
    reports = TelegramReport.objects.filter(
        created_at__gte=start_date,
        created_at__lte=end_date
    )

    total = reports.count()
    successful = reports.filter(success=True).count()

    by_type = list(reports.values('report_type').annotate(
        count=Count('id')
    ).order_by('-count'))

    by_reason = list(reports.values('reason').annotate(
        count=Count('id')
    ).order_by('-count'))

    return {
        'total': total,
        'successful': successful,
        'failed': total - successful,
        'success_rate': round(successful / total * 100, 1) if total else 0,
        'by_type': by_type,
        'by_reason': by_reason,
    }


def get_reports_by_day(start_date, end_date):
    """Get reports submitted per day."""
    return TelegramReport.objects.filter(
        created_at__gte=start_date,
        created_at__lte=end_date
    ).annotate(
        date=TruncDate('created_at')
    ).values('date').annotate(
        count=Count('id'),
        successful=Count('id', filter=Q(success=True))
    ).order_by('date')


def get_exclusion_stats():
    """Get exclusion rule stats."""
    rules = ExclusionRule.objects.filter(is_active=True)

    total = rules.count()
    global_count = rules.filter(is_global=True).count()
    total_triggers = rules.aggregate(total=Sum('trigger_count'))['total'] or 0

    # Top excluded users
    top_excluded = rules.values(
        'telegram_user__username', 'telegram_user__first_name',
        'telegram_user__last_name', 'telegram_user_id'
    ).annotate(
        rule_count=Count('id'),
        total_triggers=Sum('trigger_count')
    ).order_by('-total_triggers')[:10]

    return {
        'total_rules': total,
        'global_rules': global_count,
        'source_specific': total - global_count,
        'total_triggers': total_triggers,
        'top_excluded': list(top_excluded),
    }


def get_user_notes_count(start_date, end_date):
    """Get count of user notes created in period."""
    return UserNote.objects.filter(
        created_at__gte=start_date,
        created_at__lte=end_date
    ).count()


def get_investigation_summary(start_date, end_date):
    """Get full investigation summary for export."""
    return {
        'period': {
            'start': start_date.isoformat(),
            'end': end_date.isoformat(),
        },
        'reports': get_report_stats(start_date, end_date),
        'exclusions': get_exclusion_stats(),
        'notes_created': get_user_notes_count(start_date, end_date),
    }


# =============================================================================
# Media Inventory Queries
# =============================================================================

MEDIA_TYPES = ('photo', 'video', 'file')

SIZE_BANDS = {
    'lt_100mb':   (0,                  100 * 1024**2),
    '100mb_1gb':  (100 * 1024**2,      1024**3),
    '1gb_10gb':   (1024**3,            10 * 1024**3),
    '10gb_100gb': (10 * 1024**3,       100 * 1024**3),
    'gt_100gb':   (100 * 1024**3,      None),
}

SIZE_BAND_LABELS = {
    'lt_100mb':   '< 100 MB',
    '100mb_1gb':  '100 MB – 1 GB',
    '1gb_10gb':   '1 – 10 GB',
    '10gb_100gb': '10 – 100 GB',
    'gt_100gb':   '> 100 GB',
}


def _empty_row():
    """Per-type zero row used to seed the pivot."""
    return {
        'photo_count': 0, 'photo_size': 0,
        'video_count': 0, 'video_size': 0,
        'file_count':  0, 'file_size':  0,
        'total_size':  0,
        'last_downloaded': None,
    }


def get_source_media_inventory(media_type=None, size_band=None, account_id=None):
    """
    Per-source media inventory: one row per source that is either currently
    holding downloaded media OR has at least one ChannelConfig.download_* flag
    enabled.

    Logical-bytes accounting — duplicates are included in size totals (a
    duplicate row represents content delivered by that source, even when the
    physical file is owned by another row via DownloadedFile.original_file).

    Filters:
      - media_type: 'photo' | 'video' | 'file' | None
                    When set, the table is viewed through one type's lens:
                    sort key becomes that type's size and the size_band filter
                    applies to that type's size (not total).
      - size_band:  key from SIZE_BANDS (or None)
      - account_id: filter to a single TelegramAccount

    Returns: list of dicts shaped as::

        {
            'channel': <TelegramChannel>,
            'photo_count': int, 'photo_size': int,
            'video_count': int, 'video_size': int,
            'file_count':  int, 'file_size':  int,
            'total_size':  int,
            'last_downloaded': datetime | None,
            'configured_only': bool,  # True if no downloads yet but config flag enabled
        }
    """
    df_qs = DownloadedFile.objects.filter(channel__account__is_active=True)
    if account_id:
        df_qs = df_qs.filter(channel__account_id=account_id)

    agg = df_qs.values('channel_id', 'file_type').annotate(
        count=Count('id'),
        size=Sum('file_size'),
        latest=Max('downloaded_at'),
    )

    rows = {}
    for item in agg:
        cid = item['channel_id']
        ftype = item['file_type']
        if ftype not in MEDIA_TYPES:
            continue
        row = rows.setdefault(cid, _empty_row())
        row[f'{ftype}_count'] = item['count']
        row[f'{ftype}_size'] = item['size'] or 0
        row['total_size'] += item['size'] or 0
        if item['latest'] and (row['last_downloaded'] is None or item['latest'] > row['last_downloaded']):
            row['last_downloaded'] = item['latest']

    downloaded_ids = set(rows.keys())

    # Configured-but-empty sources (download flag enabled but no DownloadedFile yet)
    config_qs = ChannelConfig.objects.filter(
        channel__account__is_active=True,
    ).filter(
        Q(download_photos=True) | Q(download_videos=True) | Q(download_files=True),
    ).exclude(channel_id__in=downloaded_ids).values_list('channel_id', flat=True)
    if account_id:
        config_qs = config_qs.filter(channel__account_id=account_id)

    configured_only_ids = set(config_qs)
    for cid in configured_only_ids:
        rows[cid] = _empty_row()

    if not rows:
        return []

    # Hydrate channel records in one query
    channels = TelegramChannel.objects.filter(
        id__in=rows.keys()
    ).select_related('account', 'config').only(
        'id', 'title', 'username', 'channel_type',
        'account__id', 'account__display_name', 'account__phone_number',
        'config__download_photos', 'config__download_videos', 'config__download_files',
    )

    # Sort key depends on whether a media_type filter is active
    sort_key = f'{media_type}_size' if media_type in MEDIA_TYPES else 'total_size'

    # Size-band filter (against the sort key — i.e. the column being viewed)
    band = SIZE_BANDS.get(size_band) if size_band else None

    out = []
    for ch in channels:
        row = rows[ch.id]
        row['channel'] = ch
        row['configured_only'] = ch.id in configured_only_ids

        if band:
            lo, hi = band
            value = row[sort_key]
            if value < lo:
                continue
            if hi is not None and value >= hi:
                continue

        # When viewing through a single media-type lens, skip rows with zero of that type
        # unless the source is configured-only (we still want to surface misconfig).
        if media_type in MEDIA_TYPES and row[sort_key] == 0 and not row['configured_only']:
            continue

        out.append(row)

    out.sort(key=lambda r: r[sort_key], reverse=True)
    return out


def summarize_inventory_totals(rows, media_type=None):
    """
    Sum the visible rows for the filter-aware totals strip at the top of the page.

    With no media_type filter, returns per-type totals + grand total.
    With a media_type filter, the other types are still summed (always zero in
    that case because rows have been narrowed to that type's lens) and the
    grand total equals the filtered type's total.
    """
    totals = {
        'photo_size':  sum(r['photo_size']  for r in rows),
        'video_size':  sum(r['video_size']  for r in rows),
        'file_size':   sum(r['file_size']   for r in rows),
        'photo_count': sum(r['photo_count'] for r in rows),
        'video_count': sum(r['video_count'] for r in rows),
        'file_count':  sum(r['file_count']  for r in rows),
        'source_count': len(rows),
    }
    if media_type in MEDIA_TYPES:
        totals['total_size'] = totals[f'{media_type}_size']
    else:
        totals['total_size'] = totals['photo_size'] + totals['video_size'] + totals['file_size']
    return totals


def get_source_storage_trend(start_date, end_date, top_n=10, account_id=None):
    """
    Daily bytes-added per source for the top N sources by all-time total size.

    Frontend converts the daily-add series into a cumulative stacked area.
    """
    df_qs = DownloadedFile.objects.filter(channel__account__is_active=True)
    if account_id:
        df_qs = df_qs.filter(channel__account_id=account_id)

    top_ids = list(
        df_qs.values('channel_id')
        .annotate(total=Sum('file_size'))
        .order_by('-total')
        .values_list('channel_id', flat=True)[:top_n]
    )
    if not top_ids:
        return {'channels': [], 'dates': [], 'series': {}}

    daily = (
        df_qs.filter(
            channel_id__in=top_ids,
            downloaded_at__gte=start_date,
            downloaded_at__lte=end_date,
        )
        .annotate(date=TruncDate('downloaded_at'))
        .values('channel_id', 'date')
        .annotate(size=Sum('file_size'))
        .order_by('date')
    )

    channels = {
        ch.id: ch for ch in TelegramChannel.objects.filter(id__in=top_ids).only(
            'id', 'title', 'username'
        )
    }

    # Build full date axis so the frontend doesn't have to interpolate
    dates = []
    cursor = start_date.date()
    end = end_date.date()
    while cursor <= end:
        dates.append(cursor)
        cursor += timedelta(days=1)

    # series[channel_id] = list of daily bytes aligned to `dates`
    series = {cid: [0] * len(dates) for cid in top_ids}
    date_idx = {d: i for i, d in enumerate(dates)}
    for row in daily:
        idx = date_idx.get(row['date'])
        if idx is not None:
            series[row['channel_id']][idx] = row['size'] or 0

    return {
        'channels': [
            {
                'id': cid,
                'title': channels[cid].title if cid in channels else f'Source {cid}',
                'username': channels[cid].username if cid in channels else '',
            }
            for cid in top_ids
        ],
        'dates': dates,
        'series': series,
    }
