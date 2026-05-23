"""
Dashboard views.
"""

import json
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from silk.profiling.profiler import silk_profile
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate, TruncMinute
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from accounts.models import TelegramAccount
from audit.models import TelegramChannel, TelegramUser
from downloads.models import ArchivedMessage, DownloadedFile

def _get_activity_data(days):
    """Get activity data for charts over specified days."""
    start_date = timezone.now() - timedelta(days=days)

    # Messages per day
    messages_by_day = ArchivedMessage.objects.filter(
        archived_at__gte=start_date
    ).values(
        date=TruncDate('archived_at')
    ).annotate(count=Count('*')).order_by('date')

    # Downloads per day
    downloads_by_day = DownloadedFile.objects.filter(
        downloaded_at__gte=start_date
    ).values(
        date=TruncDate('downloaded_at')
    ).annotate(count=Count('*')).order_by('date')

    # New users discovered per day
    users_by_day = TelegramUser.objects.filter(
        first_seen__gte=start_date
    ).values(
        date=TruncDate('first_seen')
    ).annotate(count=Count('*')).order_by('date')

    # Build date range for labels
    date_labels = []
    messages_data = {}
    downloads_data = {}
    users_data = {}

    for i in range(days):
        date = (timezone.now() - timedelta(days=days - 1 - i)).date()
        date_labels.append(date.strftime('%b %d'))
        messages_data[date] = 0
        downloads_data[date] = 0
        users_data[date] = 0

    # Fill in actual values
    for item in messages_by_day:
        if item['date'] in messages_data:
            messages_data[item['date']] = item['count']

    for item in downloads_by_day:
        if item['date'] in downloads_data:
            downloads_data[item['date']] = item['count']

    for item in users_by_day:
        if item['date'] in users_data:
            users_data[item['date']] = item['count']

    return {
        'labels': date_labels,
        'messages': list(messages_data.values()),
        'downloads': list(downloads_data.values()),
        'users': list(users_data.values()),
    }


@login_required
@silk_profile(name='dashboard.index')
def index(request):
    """Main dashboard view - lightweight, stats loaded via HTMX."""
    # Only load active accounts for header (fast query)
    accounts = TelegramAccount.objects.active()
    connected_accounts = sum(1 for a in accounts if a.listener_status in ('running', 'flood_wait'))

    # Check for accounts in flood wait
    flood_wait_accounts = [
        a for a in accounts
        if a.flood_wait_until and a.flood_wait_until > timezone.now()
    ]

    context = {
        'accounts': accounts,
        'connected_accounts': connected_accounts,
        'flood_wait_accounts': flood_wait_accounts,
    }
    return render(request, 'dashboard/index.html', context)


@login_required
@silk_profile(name='dashboard.stats_messages')
def stats_messages(request):
    """HTMX partial: Message stats card."""
    from downloads.models import ArchivedMessage

    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=7)

    messages_base = ArchivedMessage.objects.from_active_accounts()
    total_messages = messages_base.count()
    messages_today = messages_base.filter(archived_at__gte=today_start).count()
    messages_week = messages_base.filter(archived_at__gte=week_start).count()

    return render(request, 'dashboard/partials/stats_messages.html', {
        'total_messages': total_messages,
        'messages_today': messages_today,
        'messages_week': messages_week,
    })


@login_required
@silk_profile(name='dashboard.stats_downloads')
def stats_downloads(request):
    """HTMX partial: Downloads stats card."""
    from downloads.models import DownloadedFile

    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=7)

    downloads_base = DownloadedFile.objects.filter(channel__account__is_active=True)
    total_files = downloads_base.filter(is_duplicate=False).count()
    downloads_today = downloads_base.filter(downloaded_at__gte=today_start).count()
    downloads_week = downloads_base.filter(downloaded_at__gte=week_start).count()

    return render(request, 'dashboard/partials/stats_downloads.html', {
        'total_files': total_files,
        'downloads_today': downloads_today,
        'downloads_week': downloads_week,
    })


@login_required
@silk_profile(name='dashboard.stats_secondary')
def stats_secondary(request):
    """HTMX partial: Secondary stats row (storage, sources, users)."""
    from audit.models import TelegramChannel, TelegramUser
    from downloads.models import ArchivedMessage, DownloadedFile

    now = timezone.now()
    week_start = now - timedelta(days=7)

    # Storage stats (from active accounts only)
    storage_stats = DownloadedFile.objects.filter(
        channel__account__is_active=True,
        is_duplicate=False
    ).aggregate(total_size=Sum('file_size'))
    total_storage_bytes = storage_stats['total_size'] or 0
    total_storage_gb = total_storage_bytes / (1024 * 1024 * 1024)

    # Dedup savings (from active accounts only)
    dedup_stats = DownloadedFile.objects.filter(
        channel__account__is_active=True,
        is_duplicate=True
    ).aggregate(saved_size=Sum('file_size'))
    dedup_savings = dedup_stats['saved_size'] or 0

    # Sources (from active accounts only)
    total_sources = TelegramChannel.objects.from_active_accounts().count()
    new_sources_week = TelegramChannel.objects.from_active_accounts().filter(
        created_at__gte=week_start
    ).count()

    # Users tracked
    total_telegram_users = TelegramUser.objects.count()

    # New users this week
    tele_tracked = TelegramUser.objects.filter(first_seen__gte=week_start).count()

    # Messages in last minute
    messages_last_minute = ArchivedMessage.objects.from_active_accounts().filter(
        archived_at__gte=now - timedelta(minutes=1)
    ).count()

    return render(request, 'dashboard/partials/stats_secondary.html', {
        'total_storage_bytes': total_storage_bytes,
        'total_storage_gb': round(total_storage_gb, 2),
        'dedup_savings': dedup_savings,
        'total_sources': total_sources,
        'new_sources_week': new_sources_week,
        'total_telegram_users': total_telegram_users,
        'total_telegram_users_week': tele_tracked,
        'messages_last_minute': messages_last_minute,
    })

@login_required
@silk_profile(name='dashboard.stats_queue')
def stats_queue(request):
    """HTMX partial: Download queue stats."""
    from downloads.models import DownloadTask

    base = DownloadTask.objects.all()
    queue_stats = {
        'pending': base.filter(status='pending').count(),
        'downloading': base.filter(status='downloading').count(),
        'paused': base.filter(status='paused').count(),
        'failed': base.filter(status='failed').count(),
    }

    return render(request, 'dashboard/partials/stats_queue.html', {
        'queue_stats': queue_stats,
    })


@login_required
@silk_profile(name='dashboard.stats_media_breakdown')
def stats_media_breakdown(request):
    """HTMX partial: Media breakdown chart."""
    from downloads.models import DownloadedFile

    file_type_stats = DownloadedFile.objects.filter(
        channel__account__is_active=True,
        is_duplicate=False
    ).values('file_type').annotate(
        count=Count('id'),
        total_size=Sum('file_size')
    )

    file_type_breakdown = {}
    for stat in file_type_stats:
        file_type_breakdown[stat['file_type']] = {
            'count': stat['count'],
            'total_size': stat['total_size'] or 0
        }

    total_files = sum(s['count'] for s in file_type_breakdown.values())

    # Prepare pie chart data
    pie_labels = []
    pie_data = []
    pie_colors = []
    color_map = {
        'photo': '#0dcaf0',
        'video': '#198754',
        'file': '#ffc107',
    }
    for ft, stats in file_type_breakdown.items():
        pie_labels.append(ft.capitalize() + 's')
        pie_data.append(stats['count'])
        pie_colors.append(color_map.get(ft, '#6c757d'))

    return render(request, 'dashboard/partials/stats_media_breakdown.html', {
        'file_type_breakdown': file_type_breakdown,
        'total_files': total_files,
        'pie_labels': json.dumps(pie_labels),
        'pie_data': json.dumps(pie_data),
        'pie_colors': json.dumps(pie_colors),
    })


@login_required
@silk_profile(name='dashboard.stats_accounts')
def stats_accounts(request):
    """HTMX partial: Account statuses."""
    from accounts.models import TelegramAccount

    accounts = TelegramAccount.objects.active().prefetch_related('channels')
    account_statuses = []
    for account in accounts:
        account_statuses.append({
            'account': account,
            'channel_count': account.channels.count(),
            'is_connected': account.listener_status in ('running', 'flood_wait'),
        })

    return render(request, 'dashboard/partials/stats_accounts.html', {
        'account_statuses': account_statuses,
    })


@login_required
@silk_profile(name='dashboard.activity_data')
def activity_data(request):
    """API endpoint for activity chart data with variable time range."""
    days = int(request.GET.get('days', 7))
    if days not in [1, 3, 7, 30, 90]:
        days = 7

    data = _get_activity_data(days)
    return JsonResponse(data)


@login_required
@silk_profile(name='dashboard.messages_realtime_data')
def messages_realtime_data(request):
    """API endpoint for real-time messages archived chart (minute-based)."""

    minutes = int(request.GET.get('minutes', 15))
    if minutes not in [10, 15, 30, 60]:
        minutes = 15

    now = timezone.now()
    start_time = now - timedelta(minutes=minutes)

    # Materialize channel IDs to avoid FK chain JOINs in the query
    user_channel_ids = list(
        TelegramChannel.objects.from_active_accounts()
        .filter(active=True)
        .values_list('pk', flat=True)
    )

    # Messages per minute
    messages_by_minute = ArchivedMessage.objects.filter(
        channel_id__in=user_channel_ids,
        archived_at__gte=start_time
    ).annotate(
        minute=TruncMinute('archived_at')
    ).values('minute').annotate(
        count=Count('id')
    ).order_by('minute')

    # Build minute range for labels
    labels = []
    data = {}

    for i in range(minutes):
        minute_time = start_time + timedelta(minutes=i)
        minute_time = minute_time.replace(second=0, microsecond=0)
        labels.append(minute_time.strftime('%H:%M'))
        data[minute_time.strftime('%Y-%m-%d %H:%M')] = 0

    # Fill in actual values
    for item in messages_by_minute:
        key = item['minute'].strftime('%Y-%m-%d %H:%M')
        if key in data:
            data[key] = item['count']

    return JsonResponse({
        'labels': labels,
        'data': list(data.values()),
    })


@login_required
@silk_profile(name='dashboard.total_activity_data')
def total_activity_data(request):
    """API endpoint for total activity logs chart (minute-based)."""
    from audit.models import ActivityLog

    minutes = int(request.GET.get('minutes', 15))
    if minutes not in [10, 15, 30, 60]:
        minutes = 15

    now = timezone.now()
    start_time = now - timedelta(minutes=minutes)

    # Activity logs per minute (excluding message_processed events)
    activity_by_minute = ActivityLog.objects.filter(
        timestamp__gte=start_time
    ).exclude(
        activity_type='message_processed'
    ).annotate(
        minute=TruncMinute('timestamp')
    ).values('minute').annotate(
        count=Count('id')
    ).order_by('minute')

    # Build minute range for labels
    labels = []
    data = {}

    for i in range(minutes):
        minute_time = start_time + timedelta(minutes=i)
        minute_time = minute_time.replace(second=0, microsecond=0)
        labels.append(minute_time.strftime('%H:%M'))
        data[minute_time.strftime('%Y-%m-%d %H:%M')] = 0

    # Fill in actual values
    for item in activity_by_minute:
        key = item['minute'].strftime('%Y-%m-%d %H:%M')
        if key in data:
            data[key] = item['count']

    return JsonResponse({
        'labels': labels,
        'data': list(data.values()),
    })


@login_required
@silk_profile(name='dashboard.stats_core_tasks')
def stats_core_tasks(request):
    """HTMX partial: Core scheduler tasks status."""
    from accounts.models import GlobalSettings
    from downloads.models import TaskRun

    settings = GlobalSettings.get_settings()

    # Define core tasks with their settings
    core_tasks = [
        {
            'name': 'Channel Sync',
            'task_type': 'sync_channels',
            'interval': settings.channel_sync_interval,
        },
        {
            'name': 'Channel Stats',
            'task_type': 'refresh_stats',
            'interval': settings.channel_stats_interval,
        },
        {
            'name': 'Media Counts',
            'task_type': 'media_counts',
            'interval': settings.media_counts_interval,
        },
        {
            'name': 'Stuck Recovery',
            'task_type': 'stuck_recovery',
            'interval': settings.stuck_task_recovery_interval,
        },
        {
            'name': 'Availability Check',
            'task_type': 'availability_check_all',
            'interval': settings.availability_check_interval,
        },
        {
            'name': 'Forum Topics Sync',
            'task_type': 'sync_topics_all',
            'interval': settings.forum_topics_sync_interval,
        },
    ]

    # Get last run for each task type
    for task_info in core_tasks:
        last_run = TaskRun.objects.filter(
            task_type=task_info['task_type']
        ).order_by('-completed_at').first()

        task_info['last_run'] = last_run
        task_info['is_disabled'] = task_info['interval'] == 0

        # Calculate next run time if we have a last run and interval > 0
        if last_run and last_run.completed_at and task_info['interval'] > 0:
            task_info['next_run'] = last_run.completed_at + timedelta(seconds=task_info['interval'])
        else:
            task_info['next_run'] = None

        # Format interval for display
        if task_info['interval'] == 0:
            task_info['interval_display'] = 'Disabled'
        elif task_info['interval'] < 60:
            task_info['interval_display'] = f"{task_info['interval']}s"
        elif task_info['interval'] < 3600:
            task_info['interval_display'] = f"{task_info['interval'] // 60}m"
        else:
            task_info['interval_display'] = f"{task_info['interval'] // 3600}h"

    return render(request, 'dashboard/partials/stats_core_tasks.html', {
        'core_tasks': core_tasks,
    })
