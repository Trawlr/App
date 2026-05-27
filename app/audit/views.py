"""
Audit app views (Sources/Channels).
"""

import json
import logging
import mimetypes
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from urllib.parse import urlencode
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from silk.profiling.profiler import silk_profile
from django.contrib.contenttypes.models import ContentType
from django.core.paginator import Paginator

from audit.utils import EstimatedCountPaginator
from django.db.models import (
    Case,
    Count,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from accounts.models import GlobalSettings, TelegramAccount
from accounts.telegram_service import TelegramService, run_async
from downloads.consumers import sync_broadcast_queue_update
from downloads.models import ArchivedMessage, DownloadedFile, DownloadTask, TaskRun
from reports.queries_activity import get_top_channels_for_user
from storage.utils import get_storage_backend
from tasks import (
    backfill_thumbnails,
    check_source_availability as check_source_availability_task,
    check_account_availability as check_account_availability_task,
    dispatch_task,
    fetch_media_counts,
    fetch_single_user_profile,
    fetch_user_profiles,
    scan_all_channel_members_for_user,
    scan_channel_history,
    scan_channel_members,
    sync_missing_media,
)

from .forms import ChannelConfigForm
from .models import (
    ActivityLog,
    ChannelConfig,
    ExclusionRule,
    ForwardSource,
    MessageEntity,
    TAG_COLOUR_CHOICES,
    Tag,
    TelegramChannel,
    TelegramReport,
    TelegramUser,
    UserGroupMembership,
    UserMonitoredSource,
    UserNote,
)

logger = logging.getLogger('trawlr.audit')


@login_required
@require_http_methods(['POST'])
def scan_all_members(request):
    """Trigger member scan for all sources."""
    # Count groups only (member scanning only works for groups/supergroups)
    group_count = TelegramChannel.objects.filter(
        channel_type__in=['group', 'supergroup']
    ).count()

    # Queue a single task that processes all groups with one connection per account
    # This is more efficient than individual tasks as it reuses the Telegram connection
    dispatch_task(
        scan_all_channel_members_for_user,
        task_type='scan_members',
    )

    messages.success(request, f'Member scan started for {group_count} groups. Using single connection per account.')

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'status': 'started', 'count': group_count})

    return redirect('audit:users')


@login_required
@silk_profile(name='audit.users')
def users(request):
    """List all tracked Telegram users across all sources."""
    search = request.GET.get('q', '').strip()
    photo_filter = request.GET.get('photo', 'all')
    sort = request.GET.get('sort', 'last_seen')

    # Use Exists - much faster than IN with subqueries
    user_membership_exists = UserGroupMembership.objects.filter(
        user_id=OuterRef('pk')
    )

    # Group count using direct join (not nested subquery)
    group_count_subquery = UserGroupMembership.objects.filter(
        user_id=OuterRef('pk')
    ).values('user_id').annotate(cnt=Count('id')).values('cnt')

    # Exclusion subqueries
    global_exclusion_subquery = ExclusionRule.objects.filter(
        telegram_user_id=OuterRef('pk'),
        is_global=True
    )
    source_exclusion_count_subquery = ExclusionRule.objects.filter(
        telegram_user_id=OuterRef('pk'),
        is_global=False
    ).values('telegram_user_id').annotate(cnt=Count('id')).values('cnt')

    telegram_users = TelegramUser.objects.filter(
        Exists(user_membership_exists)
    ).only(
        'id', 'telegram_id', 'username', 'first_name', 'last_name',
        'is_bot', 'is_premium', 'is_verified', 'is_scam', 'is_fake', 'last_seen'
    ).annotate(
        group_count=Subquery(group_count_subquery),
        has_global_exclusion=Exists(global_exclusion_subquery),
        source_exclusion_count=Subquery(source_exclusion_count_subquery),
        has_photo=Case(
            When(profile_photo_base64__isnull=False, profile_photo_base64__gt='', then=Value(True)),
            default=Value(False),
        )
    )

    if search:
        telegram_users = telegram_users.filter(
            Q(username__icontains=search) |
            Q(first_name__icontains=search) |
            Q(last_name__icontains=search) |
            Q(telegram_id__icontains=search)
        )

    # Apply photo filter using the has_photo annotation
    if photo_filter == 'yes':
        telegram_users = telegram_users.filter(has_photo=True)
    elif photo_filter == 'no':
        telegram_users = telegram_users.filter(has_photo=False)

    # Filter by tag
    tag_filter = request.GET.get('tag', '')
    if tag_filter:
        telegram_users = telegram_users.filter(tags__id=tag_filter)

    # Apply sorting
    if sort == 'groups':
        telegram_users = telegram_users.order_by('-group_count', '-last_seen')
    elif sort == 'groups_asc':
        telegram_users = telegram_users.order_by('group_count', '-last_seen')
    else:
        telegram_users = telegram_users.order_by('-last_seen')

    # Per page (user preference, stored in session)
    per_page = request.GET.get('per_page', request.session.get('users_per_page', 50))
    try:
        per_page = int(per_page)
        per_page = max(10, min(500, per_page))  # Clamp between 10-500
    except (ValueError, TypeError):
        per_page = 50
    request.session['users_per_page'] = per_page

    # Pagination
    paginator = Paginator(telegram_users, per_page)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    all_tags = Tag.objects.all()

    context = {
        'users': page_obj,
        'page_obj': page_obj,
        'search': search,
        'photo_filter': photo_filter,
        'tag_filter': tag_filter,
        'sort': sort,
        'per_page': per_page,
        'all_tags': all_tags,
    }
    return render(request, 'audit/users.html', context)


@login_required
@silk_profile(name='audit.user_detail')
def user_detail(request, pk):
    """View details of a specific Telegram user."""
    # Get user if they're in any of the user's channels (from active accounts only)
    user_channels = TelegramChannel.objects.from_active_accounts().all()

    telegram_user = get_object_or_404(
        TelegramUser.objects.prefetch_related('memberships__channel'),
        pk=pk
    )

    # Check user has access to this telegram user
    memberships_qs = UserGroupMembership.objects.filter(
        user=telegram_user,
        channel__in=user_channels
    ).select_related('channel').order_by('-last_seen')

    if not memberships_qs.exists():
        raise Http404("User not found")

    # Paginate memberships
    paginator = Paginator(memberships_qs, 10)
    page_number = request.GET.get('page', 1)
    memberships = paginator.get_page(page_number)
    total_memberships = paginator.count

    # Get active accounts that have seen this user (through their channels)
    seen_by_accounts = TelegramAccount.objects.filter(
        channels__in=memberships_qs.values_list('channel', flat=True)
    ).active().distinct()

    # Get history of changes - only where username or name actually changed
    all_history = list(telegram_user.history.all()[:50])
    changes = []
    for i, record in enumerate(all_history):
        if i + 1 < len(all_history):
            prev = all_history[i + 1]
            # Check if username or name changed
            if (record.username != prev.username or
                record.first_name != prev.first_name or
                record.last_name != prev.last_name):
                changes.append({
                    'date': record.history_date,
                    'old_username': prev.username,
                    'new_username': record.username,
                    'old_name': f"{prev.first_name} {prev.last_name}".strip(),
                    'new_name': f"{record.first_name} {record.last_name}".strip(),
                    'username_changed': record.username != prev.username,
                    'name_changed': (record.first_name != prev.first_name or
                                    record.last_name != prev.last_name),
                })

    # Get membership history (joins/leaves/rejoins) - batch query all at once
    # to avoid N+1 (one history query per membership).
    membership_ids = list(memberships_qs.values_list('id', flat=True))
    channels_by_membership = {m.id: m.channel for m in memberships_qs}

    HistMembership = UserGroupMembership.history.model
    all_hist = list(
        HistMembership.objects.filter(id__in=membership_ids)
        .order_by('id', '-history_date')
    )

    # Group by membership id, cap at 20 per membership
    hist_by_id = defaultdict(list)
    for record in all_hist:
        if len(hist_by_id[record.id]) < 20:
            hist_by_id[record.id].append(record)

    membership_history = []
    for m_id, hist_records in hist_by_id.items():
        channel = channels_by_membership.get(m_id)
        if not channel:
            continue
        for i, record in enumerate(hist_records):
            if i + 1 < len(hist_records):
                prev = hist_records[i + 1]
                # Check if active status changed
                if record.active != prev.active:
                    if record.active:
                        membership_history.append({
                            'date': record.history_date,
                            'channel': channel,
                            'action': 'rejoined',
                            'action_display': 'Re-joined',
                        })
                    else:
                        membership_history.append({
                            'date': record.history_date,
                            'channel': channel,
                            'action': 'left',
                            'action_display': 'Left',
                        })
            elif i == len(hist_records) - 1:
                # Earliest record - represents when they first joined
                membership_history.append({
                    'date': record.history_date,
                    'channel': channel,
                    'action': 'joined',
                    'action_display': 'Joined',
                })

    # Sort by date descending and limit
    membership_history.sort(key=lambda x: x['date'], reverse=True)
    membership_history = membership_history[:30]

    # Get existing exclusion rules for this user
    exclusions = ExclusionRule.objects.filter(
        telegram_user=telegram_user
    ).select_related('source')
    has_global_exclusion = exclusions.filter(is_global=True).exists()
    source_exclusion_count = exclusions.filter(is_global=False).count()

    # Get notes count for this user
    notes_count = UserNote.get_for_object(telegram_user).count()

    all_tags = Tag.objects.all()

    context = {
        'telegram_user': telegram_user,
        'memberships': memberships,
        'total_memberships': total_memberships,
        'changes': changes[:20],
        'membership_history': membership_history,
        'seen_by_accounts': seen_by_accounts,
        'exclusions': exclusions,
        'has_global_exclusion': has_global_exclusion,
        'source_exclusion_count': source_exclusion_count,
        'notes_count': notes_count,
        'tag_count': telegram_user.tags.count(),
        'all_tags': all_tags,
        'colour_choices': TAG_COLOUR_CHOICES,
        'active_tab': 'overview',
    }
    return render(request, 'audit/user_detail.html', context)


@login_required
@silk_profile(name='audit.sources')
def sources(request):
    """List all sources (channels/groups) across all accounts."""
    # Get user's monitored channel IDs for filtering and display
    monitored_channel_ids = set(
        UserMonitoredSource.objects.filter(user=request.user).values_list('channel_id', flat=True)
    )

    # Get all channels from user's active accounts (only active channels by default)
    all_channels = TelegramChannel.objects.from_active_accounts().filter(
        active=True
    ).select_related('account', 'config').prefetch_related('tags')

    # Filter by search query
    search = request.GET.get('search', '')
    if search:
        all_channels = all_channels.filter(
            Q(title__icontains=search) |
            Q(username__icontains=search)
        )

    # Filter by type
    channel_type = request.GET.get('type', '')
    if channel_type:
        all_channels = all_channels.filter(channel_type=channel_type)

    # Filter by account
    account_id = request.GET.get('account', '')
    if account_id:
        all_channels = all_channels.filter(account_id=account_id)

    # Filter by pinned/monitored sources (legacy parameter)
    pinned_filter = request.GET.get('pinned', '')
    if pinned_filter == '1':
        all_channels = all_channels.filter(pk__in=monitored_channel_ids)

    # Filter parameter for sidebar nav (monitored, recent)
    nav_filter = request.GET.get('filter', '')
    if nav_filter == 'monitored':
        all_channels = all_channels.filter(pk__in=monitored_channel_ids)
    elif nav_filter == 'recent':
        # Last 25 sources by joined_at date - filter out nulls
        all_channels = all_channels.exclude(joined_at__isnull=True)

    # Filter by availability status (explicit filter dropdown)
    availability_filter = request.GET.get('availability', '')
    if availability_filter:
        all_channels = all_channels.filter(availability_status=availability_filter)

    # Filter by tag
    tag_filter = request.GET.get('tag', '')
    if tag_filter:
        all_channels = all_channels.filter(tags__id=tag_filter)

    # Annotate with is_monitored for ordering (pinned first)
    all_channels = all_channels.annotate(
        is_monitored=Case(
            When(pk__in=monitored_channel_ids, then=Value(1)),
            default=Value(0),
            output_field=IntegerField()
        )
    )

    # Sort option
    sort = request.GET.get('sort', 'title')
    if nav_filter == 'recent':
        # For recently joined filter, always sort by joined_at
        sources_qs = all_channels.order_by(F('joined_at').desc(nulls_last=True))
    elif sort == 'joined':
        # Handle NULL joined_at values - push them to the end
        sources_qs = all_channels.order_by('-is_monitored', F('joined_at').desc(nulls_last=True))
    else:
        sort_options = {
            'title': ['-is_monitored', 'title'],
            'recent': ['-is_monitored', '-created_at'],
            'updated': ['-is_monitored', '-updated_at'],
        }
        sources_qs = all_channels.order_by(*sort_options.get(sort, sort_options['title']))

    # Count unavailable for badge display
    unavailable_count = all_channels.exclude(availability_status='active').count()

    # Per page (user preference, stored in session)
    # For 'recent' filter, default to 25 to show last 25 joined sources
    default_per_page = 25 if nav_filter == 'recent' else request.session.get('sources_per_page', 25)
    per_page = request.GET.get('per_page', default_per_page)
    try:
        per_page = int(per_page)
        per_page = max(10, min(500, per_page))  # Clamp between 10-500
    except (ValueError, TypeError):
        per_page = 25
    if nav_filter != 'recent':
        request.session['sources_per_page'] = per_page

    # Paginate sources
    paginator = Paginator(sources_qs, per_page)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    # Get user's active accounts for filter dropdown
    accounts = TelegramAccount.objects.active()

    # Availability status choices for filter dropdown
    availability_choices = [
        ('active', 'Active'),
        ('deleted', 'Deleted/Banned'),
        ('private', 'Private/Left'),
        ('restricted', 'Restricted'),
    ]

    # All tags for filter dropdown
    all_tags = Tag.objects.all()

    context = {
        'sources': page_obj,
        'page_obj': page_obj,
        'unavailable_count': unavailable_count,
        'search': search,
        'channel_type': channel_type,
        'account_id': account_id,
        'pinned_filter': pinned_filter,
        'nav_filter': nav_filter,
        'availability_filter': availability_filter,
        'tag_filter': tag_filter,
        'sort': sort,
        'accounts': accounts,
        'channel_types': TelegramChannel.CHANNEL_TYPE_CHOICES,
        'availability_choices': availability_choices,
        'monitored_channel_ids': monitored_channel_ids,
        'per_page': per_page,
        'all_tags': all_tags,
    }
    return render(request, 'audit/sources.html', context)


@login_required
@silk_profile(name='audit.archived_sources')
def archived_sources(request):
    """List all archived (inactive) sources."""
    # Get all archived (inactive) channels from user's active accounts
    all_channels = TelegramChannel.objects.from_active_accounts().filter(
        active=False
    ).select_related('account', 'config')

    # Filter by search query
    search = request.GET.get('search', '')
    if search:
        all_channels = all_channels.filter(
            Q(title__icontains=search) |
            Q(username__icontains=search)
        )

    # Filter by type
    channel_type = request.GET.get('type', '')
    if channel_type:
        all_channels = all_channels.filter(channel_type=channel_type)

    # Filter by account
    account_id = request.GET.get('account', '')
    if account_id:
        all_channels = all_channels.filter(account_id=account_id)

    # Filter by availability status
    availability_filter = request.GET.get('availability', '')
    if availability_filter:
        all_channels = all_channels.filter(availability_status=availability_filter)

    # Sort by title by default
    sort = request.GET.get('sort', 'title')
    sort_options = {
        'title': ['title'],
        'recent': ['-created_at'],
        'updated': ['-updated_at'],
    }
    sources_qs = all_channels.order_by(*sort_options.get(sort, sort_options['title']))

    # Per page
    per_page = request.GET.get('per_page', request.session.get('archived_sources_per_page', 25))
    try:
        per_page = int(per_page)
        per_page = max(10, min(500, per_page))
    except (ValueError, TypeError):
        per_page = 25
    request.session['archived_sources_per_page'] = per_page

    # Paginate
    paginator = Paginator(sources_qs, per_page)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    # Get user's active accounts for filter dropdown
    accounts = TelegramAccount.objects.active()

    context = {
        'sources': page_obj,
        'page_obj': page_obj,
        'search': search,
        'channel_type': channel_type,
        'account_id': account_id,
        'sort': sort,
        'accounts': accounts,
        'channel_types': TelegramChannel.CHANNEL_TYPE_CHOICES,
        'availability_choices': TelegramChannel.AVAILABILITY_STATUS_CHOICES,
        'availability_filter': availability_filter,
        'per_page': per_page,
        'is_archived_view': True,
    }
    return render(request, 'audit/archived_sources.html', context)


@login_required
@require_http_methods(['POST'])
def archive_source(request, pk):
    """Archive a source (set active=False). Optionally leave the channel on Telegram."""
    source = get_object_or_404(
        TelegramChannel.objects.from_active_accounts().select_related('account', 'config'),
        pk=pk
    )

    # Check if we should also leave the channel
    leave_channel = request.POST.get('leave_channel') == '1'

    if leave_channel and not source.has_left:
        # Connect to Telegram and leave
        service = TelegramService(source.account)

        async def do_leave():
            await service.create_client(
                source.account.api_id,
                source.account.api_hash,
                source.account.phone_number
            )
            try:
                return await service.leave_channel(source.telegram_id, username=source.username)
            finally:
                await service.disconnect()

        result = run_async(do_leave())

        if result['success']:
            source.has_left = True
        else:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'error': f'Failed to leave channel: {result["error"]}'}, status=400)
            messages.error(request, f'Failed to leave channel: {result["error"]}')
            return redirect('audit:source_detail', pk=pk)

    # Archive the source (set active=False)
    source.active = False
    source.save(update_fields=['active', 'has_left'] if leave_channel else ['active'])

    # Disable auto-download when archiving
    if hasattr(source, 'config') and source.config:
        source.config.auto_download_enabled = False
        source.config.save(update_fields=['auto_download_enabled'])

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'message': f'Archived {source.title}' + (' and left on Telegram' if leave_channel else '')
        })

    messages.success(request, f'Archived {source.title}' + (' and left on Telegram' if leave_channel else ''))
    return redirect('audit:sources')


@login_required
@require_http_methods(['POST'])
def unarchive_source(request, pk):
    """Unarchive a source (set active=True)."""
    source = get_object_or_404(
        TelegramChannel.objects.from_active_accounts().select_related('account', 'config'),
        pk=pk
    )

    # Unarchive the source (set active=True)
    source.active = True
    source.save(update_fields=['active'])

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'message': f'Unarchived {source.title}'
        })

    messages.success(request, f'Unarchived {source.title}')
    return redirect('audit:archived_sources')


@login_required
@require_http_methods(['POST'])
def check_sources_availability(request):
    """Trigger availability check for all sources using efficient batch processing."""
    # Get all active accounts for this user and dispatch batch tasks
    accounts = TelegramAccount.objects.filter(
        is_active=True,
        is_authenticated=True,
    )

    dispatched = 0
    for account in accounts:
        if not account.is_flood_wait_active:
            check_account_availability_task.send(account.pk)
            dispatched += 1

    if dispatched > 0:
        messages.success(request, f'Availability check started for {dispatched} account(s). This may take a few moments.')
    else:
        messages.warning(request, 'No active accounts available to check.')

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'status': 'started', 'accounts': dispatched})

    return redirect('audit:sources')


@login_required
@require_http_methods(['POST'])
def check_source_availability(request, pk):
    """Trigger availability check for a single source."""
    channel = get_object_or_404(
        TelegramChannel,
        pk=pk
    )

    # Check for existing running task
    if TaskRun.is_task_running('check_availability', channel=channel):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'error': f'Availability check already running for {channel.title}',
                'duplicate': True
            }, status=409)
        messages.warning(request, f'Availability check already running for {channel.title}.')
        return redirect('audit:source_detail', pk=pk)

    # Queue the task and track it with atomic dispatch
    try:
        dispatch_task(
            check_source_availability_task,
            'check_availability',
            channel=channel,
            args=(channel.pk,),
        )
    except Exception as e:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': f'Failed to queue task: {e}'}, status=500)
        messages.error(request, f'Failed to queue availability check: {e}')
        return redirect('audit:source_detail', pk=pk)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'status': 'started',
            'message': f'Availability check started for {channel.title}',
            'channel_id': channel.pk
        })

    messages.success(request, f'Availability check started for {channel.title}.')
    return redirect('audit:source_detail', pk=pk)


@login_required
@require_http_methods(['POST'])
def refresh_media_counts(request, pk):
    """Trigger refresh of Telegram media counts for a source."""
    channel = get_object_or_404(
        TelegramChannel,
        pk=pk
    )

    fetch_media_counts.send(channel.pk)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'status': 'started',
            'message': f'Fetching media counts for {channel.title}...'
        })

    messages.success(request, f'Media counts refresh started for {channel.title}.')
    return redirect('audit:source_detail', pk=pk)


@login_required
@require_http_methods(['POST'])
def source_toggle_monitor(request, pk):
    """Toggle monitoring/pinned status for a source (user-specific)."""
    channel = get_object_or_404(
        TelegramChannel,
        pk=pk
    )

    # Check if user is already monitoring this source
    monitored, created = UserMonitoredSource.objects.get_or_create(
        user=request.user,
        channel=channel
    )

    if not created:
        # Already monitoring - unpin it
        monitored.delete()
        is_monitored = False
    else:
        is_monitored = True

    return JsonResponse({
        'status': 'success',
        'is_monitored': is_monitored
    })


@login_required
@silk_profile(name='audit.source_detail')
def source_detail(request, pk):
    """View a source's details and configuration."""
    channel = get_object_or_404(
        TelegramChannel.objects.from_active_accounts().select_related('account', 'config'),
        pk=pk
    )

    # Get or create config
    config, created = ChannelConfig.objects.get_or_create(channel=channel)

    if request.method == 'POST':
        form = ChannelConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            messages.success(request, 'Configuration saved.')
            return redirect('audit:source_detail', pk=pk)
    else:
        form = ChannelConfigForm(instance=config)

    # Get file counts
    file_counts_query = DownloadedFile.objects.filter(channel=channel).values('file_type').annotate(count=Count('id'))
    file_counts = {'photos': 0, 'videos': 0, 'files': 0, 'total': 0}
    for item in file_counts_query:
        if item['file_type'] == 'photo':
            file_counts['photos'] = item['count']
        elif item['file_type'] == 'video':
            file_counts['videos'] = item['count']
        elif item['file_type'] == 'file':
            file_counts['files'] = item['count']
        file_counts['total'] += item['count']

    # Get message entity stats (URLs, mentions, hashtags)
    entity_stats = MessageEntity.objects.filter(
        channel=channel
    ).values('entity__entity_type').annotate(count=Count('id'))

    entity_counts = {}
    for item in entity_stats:
        entity_counts[item['entity__entity_type']] = item['count']

    # Get forward source stats (single query, derive total from annotations)
    forward_sources = ForwardSource.objects.filter(
        message__channel=channel
    ).values('source_type').annotate(count=Count('id'))

    forward_by_type = {}
    forward_count = 0
    for item in forward_sources:
        forward_by_type[item['source_type']] = item['count']
        forward_count += item['count']

    # Get message counts with special flags
    message_stats = ArchivedMessage.objects.filter(channel=channel).aggregate(
        total=Count('id'),
        pinned=Count('id', filter=Q(is_pinned=True)),
        with_ttl=Count('id', filter=Q(ttl_period__isnull=False)),
        noforwards=Count('id', filter=Q(noforwards=True)),
    )

    # Paginate change history - parse pagination params first to limit work
    history_per_page = request.GET.get('history_per_page', request.session.get('source_history_per_page', 5))
    try:
        history_per_page = int(history_per_page)
        history_per_page = max(5, min(100, history_per_page))  # Clamp between 5-100
    except (ValueError, TypeError):
        history_per_page = 5
    request.session['source_history_per_page'] = history_per_page

    history_page = request.GET.get('history_page', 1)
    try:
        history_page = int(history_page)
    except (ValueError, TypeError):
        history_page = 1

    # Get history of changes - only fetch the fields we compare
    _history_fields = [
        'history_date', 'title', 'username', 'member_count',
        'availability_status', 'is_verified', 'is_scam', 'is_restricted',
    ]
    # We need enough changes to fill the requested page; fetch history in a
    # limited window and stop once we have enough.
    max_changes_needed = history_per_page * history_page
    all_history = list(
        channel.history.only(*_history_fields).all()[:max_changes_needed * 3 + 1]
    )
    changes = []
    for i, record in enumerate(all_history):
        if i + 1 >= len(all_history):
            break
        prev = all_history[i + 1]
        change = {
            'date': record.history_date,
            'changes': [],
        }
        # Check title change
        if record.title != prev.title:
            change['changes'].append({
                'field': 'Title',
                'old': prev.title,
                'new': record.title,
            })
        # Check username change
        if record.username != prev.username:
            change['changes'].append({
                'field': 'Username',
                'old': f'@{prev.username}' if prev.username else '-',
                'new': f'@{record.username}' if record.username else '-',
            })
        # Check member count change
        if record.member_count != prev.member_count:
            diff = record.member_count - prev.member_count
            diff_str = f'+{diff}' if diff > 0 else str(diff)
            change['changes'].append({
                'field': 'Members',
                'old': f'{prev.member_count:,}',
                'new': f'{record.member_count:,} ({diff_str})',
            })
        # Check availability status change
        if record.availability_status != prev.availability_status:
            change['changes'].append({
                'field': 'Status',
                'old': prev.availability_status,
                'new': record.availability_status,
            })
        # Check flags
        if record.is_verified != prev.is_verified:
            change['changes'].append({
                'field': 'Verified',
                'old': 'Yes' if prev.is_verified else 'No',
                'new': 'Yes' if record.is_verified else 'No',
            })
        if record.is_scam != prev.is_scam:
            change['changes'].append({
                'field': 'Scam Flag',
                'old': 'Yes' if prev.is_scam else 'No',
                'new': 'Yes' if record.is_scam else 'No',
            })
        if record.is_restricted != prev.is_restricted:
            change['changes'].append({
                'field': 'Restricted',
                'old': 'Yes' if prev.is_restricted else 'No',
                'new': 'Yes' if record.is_restricted else 'No',
            })
        if change['changes']:
            changes.append(change)
            if len(changes) >= max_changes_needed:
                break

    history_paginator = Paginator(changes, history_per_page)
    history_page_obj = history_paginator.get_page(history_page)

    # Check if user is monitoring this source
    is_monitored = UserMonitoredSource.objects.filter(
        user=request.user,
        channel=channel
    ).exists()

    # Get account visibility info (which accounts can see this channel)
    owner_account = channel.account
    other_accounts = list(channel.seen_by_accounts.all())
    all_seeing_accounts = [owner_account] + other_accounts

    # Check if multiple accounts see this source (for recommendations)
    has_multiple_accounts = len(all_seeing_accounts) > 1

    # Get last activity (most recent message date)
    last_activity = ArchivedMessage.objects.filter(channel=channel).order_by('-telegram_date').values_list('telegram_date', flat=True).first()

    all_tags = Tag.objects.all()

    context = {
        'source': channel,
        'config': config,
        'form': form,
        'file_counts': file_counts,
        'last_activity': last_activity,
        'entity_counts': entity_counts,
        'forward_count': forward_count,
        'forward_by_type': forward_by_type,
        'message_stats': message_stats,
        'changes': history_page_obj,
        'history_page_obj': history_page_obj,
        'history_per_page': history_per_page,
        'active_tab': 'overview',
        'is_monitored': is_monitored,
        # Account visibility
        'owner_account': owner_account,
        'other_accounts': other_accounts,
        'all_seeing_accounts': all_seeing_accounts,
        'has_multiple_accounts': has_multiple_accounts,
        'all_tags': all_tags,
        'colour_choices': TAG_COLOUR_CHOICES,
    }
    return render(request, 'audit/source_detail.html', context)


@login_required
def source_config(request, pk):
    """Edit source configuration."""
    channel = get_object_or_404(
        TelegramChannel.objects.from_active_accounts().select_related('account', 'config'),
        pk=pk
    )

    config, _ = ChannelConfig.objects.get_or_create(channel=channel)

    if request.method == 'POST':
        form = ChannelConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            messages.success(request, 'Configuration saved.')
            return redirect('audit:source_config', pk=pk)
    else:
        form = ChannelConfigForm(instance=config)

    # Get file counts for stats display
    file_counts_query = DownloadedFile.objects.filter(channel=channel).values('file_type').annotate(count=Count('id'))
    file_counts = {'photos': 0, 'videos': 0, 'files': 0, 'total': 0}
    for item in file_counts_query:
        if item['file_type'] == 'photo':
            file_counts['photos'] = item['count']
        elif item['file_type'] == 'video':
            file_counts['videos'] = item['count']
        elif item['file_type'] == 'file':
            file_counts['files'] = item['count']
        file_counts['total'] += item['count']

    is_monitored = UserMonitoredSource.objects.filter(user=request.user, channel=channel).exists()
    return render(request, 'audit/source_config.html', {
        'source': channel,
        'config': config,
        'form': form,
        'file_counts': file_counts,
        'active_tab': 'config',
        'is_monitored': is_monitored,
    })


@login_required
@silk_profile(name='audit.source_files')
def source_files(request, pk):
    """Browse downloaded files for a source with pagination and captions."""
    channel = get_object_or_404(
        TelegramChannel.objects.from_active_accounts().select_related('account', 'config'),
        pk=pk
    )

    # Sort order (default: newest first)
    sort = request.GET.get('sort', 'newest')
    order = '-telegram_date' if sort == 'newest' else 'telegram_date'

    # Search filter
    search = request.GET.get('q', '').strip()

    # Media type filter
    media_filter = request.GET.get('media', '')

    # Get all downloaded files
    files = DownloadedFile.objects.filter(channel=channel)

    # Apply search filter (filename or SHA256)
    if search:
        files = files.filter(
            Q(original_filename__icontains=search) | Q(sha256_hash__icontains=search)
        )

    # Apply media type filter
    if media_filter:
        files = files.filter(file_type=media_filter)

    files = files.order_by(order)

    # Pagination (25 per page)
    paginator = Paginator(files, 25)
    page = request.GET.get('page', 1)
    posts = paginator.get_page(page)

    # Get pending downloads for this channel (to show download status)
    pending_message_ids = set(
        DownloadTask.objects.filter(
            channel=channel,
            status__in=['pending', 'downloading']
        ).values_list('message_id', flat=True)
    )

    # Get archived messages for these posts to show captions
    message_ids = [f.message_id for f in posts]
    archived_messages = {
        m.message_id: m
        for m in ArchivedMessage.objects.filter(
            channel=channel,
            message_id__in=message_ids
        )
    }

    # Find grouped_ids for files on this page, then query full album membership
    page_grouped_ids = set()
    file_to_grouped = {}
    for f in posts:
        msg = archived_messages.get(f.message_id)
        if msg and msg.grouped_id:
            page_grouped_ids.add(msg.grouped_id)
            file_to_grouped[f.pk] = msg.grouped_id

    album_info = {}
    if page_grouped_ids:
        # Get ALL messages in these albums (not just current page)
        all_album_msgs = (
            ArchivedMessage.objects.filter(channel=channel, grouped_id__in=page_grouped_ids)
            .values('grouped_id', 'message_id')
            .order_by('grouped_id', 'message_id')
        )
        # Count total items per album and get all message_ids
        album_totals = defaultdict(list)
        for row in all_album_msgs:
            album_totals[row['grouped_id']].append(row['message_id'])

        # Get downloaded files for these message_ids to find positions
        album_msg_ids = [mid for mids in album_totals.values() for mid in mids]
        downloaded_in_albums = set(
            DownloadedFile.objects.filter(channel=channel, message_id__in=album_msg_ids)
            .values_list('message_id', flat=True)
        )

        for fpk, gid in file_to_grouped.items():
            msg = archived_messages.get(next(f.message_id for f in posts if f.pk == fpk))
            all_msgs = album_totals.get(gid, [])
            downloaded_msgs = [m for m in all_msgs if m in downloaded_in_albums]
            total = len(all_msgs)
            try:
                position = all_msgs.index(msg.message_id) + 1
            except (ValueError, AttributeError):
                position = 1
            album_info[fpk] = {
                'position': position,
                'total': total,
                'downloaded': len(downloaded_msgs),
                'grouped_id': gid,
            }

    # Get or create config for template badges
    config, _ = ChannelConfig.objects.get_or_create(channel=channel)

    is_monitored = UserMonitoredSource.objects.filter(user=request.user, channel=channel).exists()
    context = {
        'source': channel,
        'config': config,
        'posts': posts,
        'sort': sort,
        'search': search,
        'media_filter': media_filter,
        'pending_message_ids': pending_message_ids,
        'archived_messages': archived_messages,
        'album_info': album_info,
        'active_tab': 'files',
        'is_monitored': is_monitored,
    }
    return render(request, 'audit/source_files.html', context)


@login_required
@silk_profile(name='audit.source_files_gallery')
def source_files_gallery(request, pk):
    """Gallery view for downloaded files."""
    channel = get_object_or_404(
        TelegramChannel.objects.from_active_accounts().select_related('account', 'config'),
        pk=pk
    )

    sort = request.GET.get('sort', 'newest')
    order = '-telegram_date' if sort == 'newest' else 'telegram_date'
    search = request.GET.get('q', '').strip()
    media_filter = request.GET.get('media', '')
    highlight_msg = request.GET.get('msg', '')
    album_filter = request.GET.get('album', '')

    files = DownloadedFile.objects.filter(channel=channel)

    # Album filter: show only files belonging to a specific album
    album_context = None
    if album_filter:
        try:
            album_gid = int(album_filter)
        except (ValueError, TypeError):
            album_gid = None

        if album_gid:
            album_msg_ids = list(
                ArchivedMessage.objects.filter(channel=channel, grouped_id=album_gid)
                .values_list('message_id', flat=True)
                .order_by('message_id')
            )
            files = files.filter(message_id__in=album_msg_ids)
            album_context = {'grouped_id': album_gid, 'total': len(album_msg_ids)}

    if search:
        files = files.filter(
            Q(original_filename__icontains=search) | Q(sha256_hash__icontains=search)
        )

    if media_filter:
        files = files.filter(file_type=media_filter)

    files = files.order_by(order)

    # No pagination when viewing a specific album, otherwise 60 per page
    if album_filter:
        posts = files
        paginate = False
    else:
        paginator = Paginator(files, 60)
        page = request.GET.get('page', 1)
        posts = paginator.get_page(page)
        paginate = True

    # Get archived messages for captions and grouped_id
    message_ids = [f.message_id for f in posts]
    archived_messages = {
        m.message_id: m
        for m in ArchivedMessage.objects.filter(
            channel=channel,
            message_id__in=message_ids
        )
    }

    # Find grouped_ids for files on this page, then query full album membership
    page_grouped_ids = set()
    file_to_grouped = {}
    for f in posts:
        msg = archived_messages.get(f.message_id)
        if msg and msg.grouped_id:
            page_grouped_ids.add(msg.grouped_id)
            file_to_grouped[f.pk] = msg.grouped_id

    album_info = {}
    if page_grouped_ids:
        all_album_msgs = (
            ArchivedMessage.objects.filter(channel=channel, grouped_id__in=page_grouped_ids)
            .values('grouped_id', 'message_id')
            .order_by('grouped_id', 'message_id')
        )
        album_totals = defaultdict(list)
        for row in all_album_msgs:
            album_totals[row['grouped_id']].append(row['message_id'])

        album_msg_ids = [mid for mids in album_totals.values() for mid in mids]
        downloaded_in_albums = set(
            DownloadedFile.objects.filter(channel=channel, message_id__in=album_msg_ids)
            .values_list('message_id', flat=True)
        )

        for fpk, gid in file_to_grouped.items():
            file_msg_id = next(f.message_id for f in posts if f.pk == fpk)
            all_msgs = album_totals.get(gid, [])
            total = len(all_msgs)
            try:
                position = all_msgs.index(file_msg_id) + 1
            except ValueError:
                position = 1
            album_info[fpk] = {
                'position': position,
                'total': total,
                'grouped_id': gid,
            }

    config, _ = ChannelConfig.objects.get_or_create(channel=channel)
    is_monitored = UserMonitoredSource.objects.filter(user=request.user, channel=channel).exists()

    context = {
        'source': channel,
        'config': config,
        'posts': posts,
        'sort': sort,
        'search': search,
        'media_filter': media_filter,
        'archived_messages': archived_messages,
        'album_info': album_info,
        'highlight_msg': highlight_msg,
        'album_filter': album_filter,
        'album_context': album_context,
        'paginate': paginate,
        'active_tab': 'files',
        'is_monitored': is_monitored,
    }
    return render(request, 'audit/source_files_gallery.html', context)


@login_required
@require_http_methods(['POST'])
def source_scan(request, pk):
    """Trigger historical scan for a source."""
    channel = get_object_or_404(
        TelegramChannel,
        pk=pk
    )

    # Check for existing running task
    existing_task = TaskRun.get_running_task('scan_history', channel=channel)
    if existing_task:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'error': f'History scan already running for {channel.title}',
                'duplicate': True,
                'task_id': existing_task.task_id,
                'progress': existing_task.progress_percent
            }, status=409)
        messages.warning(request, f'History scan already running for {channel.title}.')
        return redirect('audit:source_detail', pk=pk)

    # Check thumbnail option
    skip_thumbnails = request.POST.get('skip_thumbnails') == 'true'

    # Queue the task and track it with atomic dispatch
    try:
        dispatch_task(
            scan_channel_history,
            'scan_history',
            channel=channel,
            args=(channel.pk,),
            kwargs={'skip_thumbnails': skip_thumbnails},
        )
    except Exception as e:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': f'Failed to queue task: {e}'}, status=500)
        messages.error(request, f'Failed to queue history scan: {e}')
        return redirect('audit:source_detail', pk=pk)

    # Return JSON for AJAX requests
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'status': 'started',
            'message': f'History scan started for {channel.title}',
            'channel_id': channel.pk
        })

    messages.success(request, f'Historical scan started for {channel.title}. This may take a while depending on the channel size.')
    return redirect('audit:source_detail', pk=pk)


@login_required
@require_http_methods(['POST'])
def source_sync_missing_media(request, pk):
    """
    Create download tasks for archived messages that have media but no download.
    This is a DATABASE-ONLY operation - no Telegram API calls, no flood wait risk.
    """
    channel = get_object_or_404(
        TelegramChannel,
        pk=pk,
    )

    # Check for existing running task
    existing_task = TaskRun.get_running_task('sync_missing_media', channel=channel)
    if existing_task:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'error': f'Media sync already running for {channel.title}',
                'duplicate': True,
                'task_id': existing_task.task_id,
                'progress': existing_task.progress_percent
            }, status=409)
        messages.warning(request, f'Media sync already running for {channel.title}.')
        return redirect('audit:source_detail', pk=pk)

    # Check if auto-download is enabled
    config = channel.config
    if not config.auto_download_enabled:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'error': 'Auto-download is not enabled for this source. Enable it in the config first.'
            }, status=400)
        messages.error(request, 'Auto-download is not enabled for this source. Enable it in the config first.')
        return redirect('audit:source_detail', pk=pk)

    if not (config.download_photos or config.download_videos or config.download_files):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'error': 'No media types enabled for download. Enable at least one in the config.'
            }, status=400)
        messages.error(request, 'No media types enabled for download. Enable at least one in the config.')
        return redirect('audit:source_detail', pk=pk)

    # Queue the task
    try:
        dispatch_task(
            sync_missing_media,
            'sync_missing_media',
            channel=channel,
            args=(channel.pk,),
        )
    except Exception as e:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': f'Failed to queue task: {e}'}, status=500)
        messages.error(request, f'Failed to queue media sync: {e}')
        return redirect('audit:source_detail', pk=pk)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'status': 'started',
            'message': f'Media sync started for {channel.title}',
            'channel_id': channel.pk
        })

    messages.success(request, f'Media sync started for {channel.title}. Download tasks will be created for missing media.')
    return redirect('audit:source_detail', pk=pk)


@login_required
@require_http_methods(['POST'])
def source_backfill_thumbnails(request, pk):
    """Backfill thumbnails for existing posts that have media but no thumbnail."""
    channel = get_object_or_404(
        TelegramChannel,
        pk=pk
    )

    # Check for existing running task
    existing_task = TaskRun.objects.filter(
        task_type='backfill_thumbnails',
        channel=channel,
        status__in=['pending', 'running']
    ).first()

    if existing_task:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'error': f'Thumbnail backfill already running for {channel.title}',
                'duplicate': True,
                'task_id': existing_task.task_id,
                'progress': existing_task.progress_percent
            }, status=409)
        messages.warning(request, f'Thumbnail backfill already running for {channel.title}.')
        return redirect('audit:source_detail', pk=pk)

    # Queue the task with atomic dispatch
    try:
        dispatch_task(
            backfill_thumbnails,
            'backfill_thumbnails',
            channel=channel,
            args=(channel.pk,),
        )
    except Exception as e:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': f'Failed to queue task: {e}'}, status=500)
        messages.error(request, f'Failed to queue thumbnail backfill: {e}')
        return redirect('audit:source_detail', pk=pk)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'status': 'started',
            'message': f'Thumbnail backfill started for {channel.title}',
            'channel_id': channel.pk
        })

    messages.success(request, f'Thumbnail backfill started for {channel.title}.')
    return redirect('audit:source_detail', pk=pk)


@login_required
@require_http_methods(['POST'])
def source_pause(request, pk):
    """Pause/unpause downloads for a source."""
    channel = get_object_or_404(
        TelegramChannel,
        pk=pk
    )

    config, _ = ChannelConfig.objects.get_or_create(channel=channel)
    config.is_paused = not config.is_paused
    config.save()

    # Update pending_reason on all pending tasks for this channel
    if config.is_paused:
        DownloadTask.objects.filter(
            channel=channel,
            status='pending'
        ).update(pending_reason='channel_paused')
    else:
        DownloadTask.objects.filter(
            channel=channel,
            status='pending',
            pending_reason='channel_paused'
        ).update(pending_reason='')

    status = 'paused' if config.is_paused else 'resumed'

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'status': status,
            'message': f'Downloads {status} for {channel.title}',
            'update': {'is_paused': config.is_paused}
        })

    messages.success(request, f'Downloads {status} for {channel.title}')
    return redirect('audit:source_detail', pk=pk)


@login_required
@require_http_methods(['POST'])
def source_toggle_auto(request, pk):
    """Toggle auto-download for a source."""
    channel = get_object_or_404(
        TelegramChannel,
        pk=pk
    )

    config, _ = ChannelConfig.objects.get_or_create(channel=channel)
    config.auto_download_enabled = not config.auto_download_enabled
    config.save()

    status = 'enabled' if config.auto_download_enabled else 'disabled'

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'status': status,
            'message': f'Auto-download {status} for {channel.title}',
            'update': {'auto_download': config.auto_download_enabled}
        })

    messages.success(request, f'Auto-download {status} for {channel.title}')
    return redirect('audit:source_detail', pk=pk)


@login_required
@require_http_methods(['POST'])
def source_scan_members(request, pk):
    """Trigger member scan for a source."""
    channel = get_object_or_404(
        TelegramChannel,
        pk=pk
    )

    # Check for existing running task
    existing_task = TaskRun.get_running_task('scan_members', channel=channel)
    if existing_task:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'error': f'Member scan already running for {channel.title}',
                'duplicate': True,
                'task_id': existing_task.task_id,
                'progress': existing_task.progress_percent
            }, status=409)
        messages.warning(request, f'Member scan already running for {channel.title}.')
        return redirect('audit:source_detail', pk=pk)

    # Check if profile photos should be skipped
    skip_profile_photos = request.POST.get('skip_profile_photos') == 'true'

    # Queue the task and track it with atomic dispatch
    try:
        dispatch_task(
            scan_channel_members,
            'scan_members',
            channel=channel,
            args=(channel.pk,),
            kwargs={'skip_profile_photos': skip_profile_photos},
        )
    except Exception as e:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': f'Failed to queue task: {e}'}, status=500)
        messages.error(request, f'Failed to queue member scan: {e}')
        return redirect('audit:source_detail', pk=pk)

    # Return JSON for AJAX requests
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'status': 'started',
            'message': f'User scan started for {channel.title}' + (' (without profile photos)' if skip_profile_photos else ''),
            'channel_id': channel.pk
        })

    messages.success(request, f'Member scan started for {channel.title}. Users will be tracked as they are found.')
    return redirect('audit:source_detail', pk=pk)


@login_required
@require_http_methods(['POST'])
def source_fetch_profiles(request, pk):
    """Fetch full user profiles (bio, etc.) for all members in a source."""
    channel = get_object_or_404(
        TelegramChannel,
        pk=pk
    )

    # Check for existing running task
    existing_task = TaskRun.get_running_task('fetch_profiles', channel=channel)
    if existing_task:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'error': f'Profile fetch already running for {channel.title}',
                'duplicate': True,
                'task_id': existing_task.task_id,
                'progress': existing_task.progress_percent
            }, status=409)
        messages.warning(request, f'Profile fetch already running for {channel.title}.')
        return redirect('audit:source_detail', pk=pk)

    # Queue the task and track it with atomic dispatch
    try:
        dispatch_task(
            fetch_user_profiles,
            'fetch_profiles',
            channel=channel,
            args=(channel.pk,),
        )
    except Exception as e:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': f'Failed to queue task: {e}'}, status=500)
        messages.error(request, f'Failed to queue profile fetch: {e}')
        return redirect('audit:source_detail', pk=pk)

    # Return JSON for AJAX requests
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'status': 'started',
            'message': f'Profile fetch started for {channel.title}',
            'channel_id': channel.pk
        })

    messages.success(request, f'Profile fetch started for {channel.title}. Bio and other profile data will be collected.')
    return redirect('audit:source_detail', pk=pk)


@login_required
@silk_profile(name='audit.source_users')
def source_users(request, pk):
    """View tracked users for a source."""
    channel = get_object_or_404(
        TelegramChannel.objects.from_active_accounts().select_related('account', 'config'),
        pk=pk
    )

    # Get or create config for template badges
    config, _ = ChannelConfig.objects.get_or_create(channel=channel)

    # Search query
    search = request.GET.get('q', '').strip()
    posters_only = request.GET.get('posters') == '1'

    memberships = UserGroupMembership.objects.filter(
        channel=channel
    ).select_related('user').order_by('-last_seen')

    if search:
        memberships = memberships.filter(
            Q(user__username__icontains=search) |
            Q(user__first_name__icontains=search) |
            Q(user__last_name__icontains=search)
        )

    if posters_only:
        # message_count is incremented on every archived message
        # (audit/user_tracking.py), so this is a single-field index-friendly
        # check with no join.
        memberships = memberships.filter(message_count__gt=0)

    # Pagination
    paginator = Paginator(memberships, 50)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    is_monitored = UserMonitoredSource.objects.filter(user=request.user, channel=channel).exists()
    context = {
        'source': channel,
        'config': config,
        'memberships': page_obj,
        'page_obj': page_obj,
        'search': search,
        'posters_only': posters_only,
        'active_tab': 'users',
        'is_monitored': is_monitored,
    }
    return render(request, 'audit/source_users.html', context)


@login_required
def source_activity(request, pk):
    """Activity Map tab for a source — temporal pattern-of-life heatmap."""
    channel = get_object_or_404(
        TelegramChannel.objects.from_active_accounts().select_related('account', 'config'),
        pk=pk,
    )
    config, _ = ChannelConfig.objects.get_or_create(channel=channel)
    is_monitored = UserMonitoredSource.objects.filter(user=request.user, channel=channel).exists()

    return render(request, 'audit/source_activity.html', {
        'source': channel,
        'config': config,
        'active_tab': 'activity',
        'is_monitored': is_monitored,
    })


@login_required
@silk_profile(name='audit.user_posts')
def user_posts(request, pk):
    """Posts tab — archived messages posted by this user across all sources."""
    telegram_user = get_object_or_404(TelegramUser, pk=pk)
    user_channels = TelegramChannel.objects.from_active_accounts().all()

    memberships_qs = UserGroupMembership.objects.filter(
        user=telegram_user, channel__in=user_channels
    ).select_related('channel')

    if not memberships_qs.exists():
        raise Http404("User not found")

    user_channel_ids = list(memberships_qs.values_list('channel_id', flat=True))

    # Broadcast channels: the listener stores ArchivedMessage.sender_id in
    # Bot-API peer form (-100<channel_id>) while the TelegramUser record
    # synthesised for that channel keeps the raw positive ID. Match both
    # forms so channel-as-poster messages aren't lost.
    candidate_sender_ids = [telegram_user.telegram_id]
    if telegram_user.telegram_id and telegram_user.telegram_id > 0:
        candidate_sender_ids.append(-int(f"100{telegram_user.telegram_id}"))

    sort = request.GET.get('sort', 'newest')
    ordering = ('-telegram_date', '-message_id') if sort == 'newest' else ('telegram_date', 'message_id')
    media_filter = request.GET.get('media', '')
    search = request.GET.get('q', '').strip()
    channel_filter = request.GET.get('channel', '').strip()
    # Back-compat with the old ?source=<pk> param used by the previous view.
    if not channel_filter:
        channel_filter = request.GET.get('source', '').strip()
    per_page = request.GET.get('per_page', request.session.get('user_posts_per_page', 25))
    try:
        per_page = int(per_page)
        per_page = max(10, min(100, per_page))
    except (ValueError, TypeError):
        per_page = 25
    request.session['user_posts_per_page'] = per_page

    # Uses the (sender_id, telegram_date) composite index on ArchivedMessage
    # for index-ordered output.
    posts_qs = ArchivedMessage.objects.filter(
        sender_id__in=candidate_sender_ids,
        channel_id__in=user_channel_ids,
    ).select_related('channel', 'channel__account', 'downloaded_file', 'topic')

    if channel_filter.isdigit():
        posts_qs = posts_qs.filter(channel_id=int(channel_filter))

    if search:
        q_filter = (
            Q(text__icontains=search) |
            Q(sender_username__icontains=search) |
            Q(sender_name__icontains=search) |
            Q(original_filename__icontains=search) |
            Q(channel__title__icontains=search)
        )
        if search.isdigit():
            q_filter |= Q(message_id=int(search))
        posts_qs = posts_qs.filter(q_filter)

    if media_filter == 'media':
        posts_qs = posts_qs.filter(has_media=True)
    elif media_filter == 'text':
        posts_qs = posts_qs.filter(has_media=False)
    elif media_filter in ['photo', 'video', 'file']:
        posts_qs = posts_qs.filter(media_type=media_filter)

    posts_qs = posts_qs.order_by(*ordering)

    paginator = Paginator(posts_qs, per_page)
    posts_page = paginator.get_page(request.GET.get('page', 1))

    visible_channel_ids = {p.channel_id for p in posts_page}
    visible_message_ids = [p.message_id for p in posts_page]
    pending_message_ids = set()
    if visible_channel_ids and visible_message_ids:
        pending_message_ids = set(
            DownloadTask.objects.filter(
                channel_id__in=visible_channel_ids,
                message_id__in=visible_message_ids,
                status__in=['pending', 'downloading'],
            ).values_list('message_id', flat=True)
        )

    sender_ids = [p.sender_id for p in posts_page if p.sender_id]
    known_users = {}
    if sender_ids:
        known_users = {
            u.telegram_id: u.pk
            for u in TelegramUser.objects.filter(telegram_id__in=sender_ids)
        }

    album_info = {}
    grouped_pairs = {(p.channel_id, p.grouped_id) for p in posts_page if p.grouped_id}
    if grouped_pairs:
        channel_to_grouped = defaultdict(set)
        for channel_id, grouped_id in grouped_pairs:
            channel_to_grouped[channel_id].add(grouped_id)
        album_filter = Q()
        for channel_id, grouped_ids in channel_to_grouped.items():
            album_filter |= Q(channel_id=channel_id, grouped_id__in=grouped_ids)
        album_messages = ArchivedMessage.objects.filter(album_filter).values(
            'channel_id', 'grouped_id', 'pk', 'message_id'
        ).order_by('channel_id', 'grouped_id', 'message_id')

        albums = defaultdict(list)
        for msg in album_messages:
            albums[(msg['channel_id'], msg['grouped_id'])].append(msg['pk'])

        for (channel_id, grouped_id), pks in albums.items():
            total = len(pks)
            for idx, pk_val in enumerate(pks, 1):
                album_info[pk_val] = {
                    'position': idx,
                    'total': total,
                    'grouped_id': grouped_id,
                    'channel_pk': channel_id,
                }

    post_source_ids = [m.channel_id for m in memberships_qs if m.message_count > 0]
    channels = TelegramChannel.objects.filter(pk__in=post_source_ids).order_by('title')

    return render(request, 'audit/user_posts.html', {
        'telegram_user': telegram_user,
        'posts': posts_page,
        'posts_total': paginator.count,
        'channels': channels,
        'channel_filter': channel_filter,
        'sort': sort,
        'media_filter': media_filter,
        'search': search,
        'per_page': per_page,
        'pending_message_ids': pending_message_ids,
        'known_users': known_users,
        'album_info': album_info,
        'active_tab': 'posts',
    })


@login_required
def user_activity(request, pk):
    """Activity Map tab for a TelegramUser — heatmap of their posts across channels."""
    telegram_user = get_object_or_404(TelegramUser, pk=pk)
    user_channels = TelegramChannel.objects.from_active_accounts().all()
    if not UserGroupMembership.objects.filter(
        user=telegram_user, channel__in=user_channels
    ).exists():
        raise Http404("User not found")

    # Top channels for the per-channel sidebar checklist (last 30 days, matches default).
    end_date = timezone.now()
    start_date = end_date - timedelta(days=30)
    top_channels = get_top_channels_for_user(telegram_user.pk, start_date, end_date, limit=15)

    return render(request, 'audit/user_activity.html', {
        'telegram_user': telegram_user,
        'top_channels': top_channels,
        'active_tab': 'activity',
    })


@login_required
@silk_profile(name='audit.user_network')
def user_network(request, pk):
    """
    Network tab — relationship & content-flow views for a TelegramUser.

    Sibling to the Activity heatmap. Where the Activity tab answers
    "WHEN does this user post" with a calendar/hour-of-day grid, this tab
    answers "WHAT GROUPS / WHO ELSE / WHAT FLOWS" with three panels:
      1. Group activity swim lanes (one row per group, message pins on a
         shared time axis — same visual language as CIB amplifier chains)
      2. Content flow sankey (inbound forward sources -> user -> outbound
         destinations that re-broadcast the user's content)
      3. Co-poster mini-graph (other users posting same content within a
         tight time window)

    All three panels fetch from /reports/api/user/<pk>/{lanes,flow,copost}/.
    """
    telegram_user = get_object_or_404(TelegramUser, pk=pk)
    user_channels = TelegramChannel.objects.from_active_accounts().all()
    if not UserGroupMembership.objects.filter(
        user=telegram_user, channel__in=user_channels
    ).exists():
        raise Http404("User not found")

    return render(request, 'audit/user_network.html', {
        'telegram_user': telegram_user,
        'active_tab': 'network',
    })


@login_required
def source_posts(request, pk):
    """
    Default posts view - uses Table/List Hybrid layout (v3).
    """
    return source_posts_v3(request, pk)


@login_required
def source_posts_v2(request, pk):
    """Timeline/Chat view for posts."""
    channel = get_object_or_404(
        TelegramChannel.objects.from_active_accounts().select_related('account', 'config'),
        pk=pk
    )

    sort = request.GET.get('sort', 'newest')
    # Use message_id as secondary sort for deterministic ordering of same-timestamp posts
    ordering = ('-telegram_date', '-message_id') if sort == 'newest' else ('telegram_date', 'message_id')
    media_filter = request.GET.get('media', '')
    search = request.GET.get('q', '').strip()
    per_page = request.GET.get('per_page', request.session.get('posts_per_page', 25))
    try:
        per_page = int(per_page)
        per_page = max(10, min(100, per_page))
    except (ValueError, TypeError):
        per_page = 25
    request.session['posts_per_page'] = per_page

    posts_qs = ArchivedMessage.objects.filter(channel=channel).select_related('topic')

    if search:
        q_filter = (
            Q(text__icontains=search) |
            Q(sender_username__icontains=search) |
            Q(sender_name__icontains=search) |
            Q(original_filename__icontains=search)
        )
        if search.isdigit():
            q_filter |= Q(message_id=int(search))
        posts_qs = posts_qs.filter(q_filter)

    if media_filter == 'media':
        posts_qs = posts_qs.filter(has_media=True)
    elif media_filter == 'text':
        posts_qs = posts_qs.filter(has_media=False)
    elif media_filter in ['photo', 'video', 'file']:
        posts_qs = posts_qs.filter(media_type=media_filter)

    posts_qs = posts_qs.order_by(*ordering)

    # Handle goto_msg parameter - find message and redirect to correct page
    goto_msg = request.GET.get('goto_msg', '').strip()
    if goto_msg and goto_msg.isdigit():
        goto_msg_id = int(goto_msg)
        target_msg = ArchivedMessage.objects.filter(
            channel=channel,
            message_id=goto_msg_id
        ).first()

        if target_msg:
            if sort == 'newest':
                position = ArchivedMessage.objects.filter(
                    channel=channel,
                    telegram_date__gt=target_msg.telegram_date
                ).count()
            else:
                position = ArchivedMessage.objects.filter(
                    channel=channel,
                    telegram_date__lt=target_msg.telegram_date
                ).count()

            target_page = (position // per_page) + 1
            params = {
                'page': target_page,
                'sort': sort,
                'per_page': per_page,
            }
            redirect_url = f"?{urlencode(params)}#msg-{goto_msg_id}"
            return HttpResponseRedirect(redirect_url)

    paginator = Paginator(posts_qs, per_page)
    page = request.GET.get('page', 1)
    posts = paginator.get_page(page)

    pending_message_ids = set(
        DownloadTask.objects.filter(
            channel=channel,
            status__in=['pending', 'downloading']
        ).values_list('message_id', flat=True)
    )

    config, _ = ChannelConfig.objects.get_or_create(channel=channel)

    sender_ids = [p.sender_id for p in posts if p.sender_id]
    known_users = {}
    if sender_ids:
        known_users = {
            u.telegram_id: u.pk
            for u in TelegramUser.objects.filter(telegram_id__in=sender_ids)
        }

    # Calculate album positions for posts with grouped_id
    album_info = {}
    grouped_ids = [p.grouped_id for p in posts if p.grouped_id]
    if grouped_ids:
        album_messages = ArchivedMessage.objects.filter(
            channel=channel,
            grouped_id__in=grouped_ids
        ).values('grouped_id', 'pk', 'message_id').order_by('grouped_id', 'message_id')

        albums = defaultdict(list)
        for msg in album_messages:
            albums[msg['grouped_id']].append(msg['pk'])

        for grouped_id, pks in albums.items():
            total = len(pks)
            for idx, pk in enumerate(pks, 1):
                album_info[pk] = {'position': idx, 'total': total, 'grouped_id': grouped_id}

    is_monitored = UserMonitoredSource.objects.filter(user=request.user, channel=channel).exists()
    context = {
        'source': channel,
        'config': config,
        'posts': posts,
        'sort': sort,
        'media_filter': media_filter,
        'per_page': per_page,
        'search': search,
        'pending_message_ids': pending_message_ids,
        'known_users': known_users,
        'album_info': album_info,
        'active_tab': 'posts',
        'is_monitored': is_monitored,
    }
    return render(request, 'audit/source_posts_v2.html', context)


@login_required
@silk_profile(name='audit.source_posts_feed')
def source_posts_feed(request, pk):
    """
    Infinite scroll feed view for posts.
    Returns full page on initial load, HTMX partial on subsequent requests.
    Uses cursor-based pagination via telegram_date + message_id for smooth scrolling.
    """
    channel = get_object_or_404(
        TelegramChannel.objects.from_active_accounts().select_related('account', 'config'),
        pk=pk
    )

    per_page = 20

    posts_qs = ArchivedMessage.objects.filter(
        channel=channel,
    ).select_related('topic', 'downloaded_file').order_by('-telegram_date', '-message_id')

    # Cursor-based pagination: load posts older than cursor
    cursor_date = request.GET.get('cursor_date')
    cursor_id = request.GET.get('cursor_id')
    if cursor_date and cursor_id:
        try:
            cursor_dt = datetime.fromtimestamp(float(cursor_date), tz=dt_timezone.utc)
            cursor_msg_id = int(cursor_id)
            posts_qs = posts_qs.filter(
                Q(telegram_date__lt=cursor_dt) |
                Q(telegram_date=cursor_dt, message_id__lt=cursor_msg_id)
            )
        except (ValueError, TypeError):
            pass

    posts = list(posts_qs[:per_page + 1])
    has_more = len(posts) > per_page
    posts = posts[:per_page]

    # Build next cursor from last post
    next_cursor_date = None
    next_cursor_id = None
    if has_more and posts:
        last = posts[-1]
        next_cursor_date = str(last.telegram_date.timestamp())
        next_cursor_id = last.message_id

    # Known users for sender links
    sender_ids = [p.sender_id for p in posts if p.sender_id]
    known_users = {}
    if sender_ids:
        known_users = {
            u.telegram_id: u.pk
            for u in TelegramUser.objects.filter(telegram_id__in=sender_ids)
        }

    pending_message_ids = set(
        DownloadTask.objects.filter(
            channel=channel,
            status__in=['pending', 'downloading']
        ).values_list('message_id', flat=True)
    )

    config, _ = ChannelConfig.objects.get_or_create(channel=channel)
    is_monitored = UserMonitoredSource.objects.filter(user=request.user, channel=channel).exists()

    context = {
        'source': channel,
        'config': config,
        'posts': posts,
        'known_users': known_users,
        'pending_message_ids': pending_message_ids,
        'has_more': has_more,
        'next_cursor_date': next_cursor_date,
        'next_cursor_id': next_cursor_id,
        'active_tab': 'posts',
        'is_monitored': is_monitored,
    }

    # HTMX request = return just the posts partial
    if request.headers.get('HX-Request'):
        return render(request, 'audit/partials/feed_posts.html', context)

    return render(request, 'audit/source_posts_feed.html', context)


@login_required
@silk_profile(name='audit.source_posts_v3')
def source_posts_v3(request, pk):
    """Table/List Hybrid view for posts."""
    channel = get_object_or_404(
        TelegramChannel.objects.from_active_accounts().select_related('account', 'config'),
        pk=pk
    )

    sort = request.GET.get('sort', 'newest')
    # Use message_id as secondary sort for deterministic ordering of same-timestamp posts
    ordering = ('-telegram_date', '-message_id') if sort == 'newest' else ('telegram_date', 'message_id')
    media_filter = request.GET.get('media', '')
    search = request.GET.get('q', '').strip()
    per_page = request.GET.get('per_page', request.session.get('posts_per_page', 25))
    try:
        per_page = int(per_page)
        per_page = max(10, min(100, per_page))
    except (ValueError, TypeError):
        per_page = 25
    request.session['posts_per_page'] = per_page

    posts_qs = ArchivedMessage.objects.filter(channel=channel).select_related('topic')

    if search:
        q_filter = (
            Q(text__icontains=search) |
            Q(sender_username__icontains=search) |
            Q(sender_name__icontains=search) |
            Q(original_filename__icontains=search)
        )
        if search.isdigit():
            q_filter |= Q(message_id=int(search))
        posts_qs = posts_qs.filter(q_filter)

    if media_filter == 'media':
        posts_qs = posts_qs.filter(has_media=True)
    elif media_filter == 'text':
        posts_qs = posts_qs.filter(has_media=False)
    elif media_filter in ['photo', 'video', 'file']:
        posts_qs = posts_qs.filter(media_type=media_filter)

    posts_qs = posts_qs.order_by(*ordering)

    # Handle goto_msg parameter - find message and redirect to correct page
    goto_msg = request.GET.get('goto_msg', '').strip()
    if goto_msg and goto_msg.isdigit():
        goto_msg_id = int(goto_msg)
        # Find the target message (without filters to ensure we find it)
        target_msg = ArchivedMessage.objects.filter(
            channel=channel,
            message_id=goto_msg_id
        ).first()

        if target_msg:
            # Count how many messages come before this one in the current sort order
            if sort == 'newest':
                position = ArchivedMessage.objects.filter(
                    channel=channel,
                    telegram_date__gt=target_msg.telegram_date
                ).count()
            else:
                position = ArchivedMessage.objects.filter(
                    channel=channel,
                    telegram_date__lt=target_msg.telegram_date
                ).count()

            # Calculate page number (1-indexed)
            target_page = (position // per_page) + 1
            params = {
                'page': target_page,
                'sort': sort,
                'per_page': per_page,
            }
            redirect_url = f"?{urlencode(params)}#msg-{goto_msg_id}"
            return HttpResponseRedirect(redirect_url)

    paginator = Paginator(posts_qs, per_page)
    page = request.GET.get('page', 1)
    posts = paginator.get_page(page)

    pending_message_ids = set(
        DownloadTask.objects.filter(
            channel=channel,
            status__in=['pending', 'downloading']
        ).values_list('message_id', flat=True)
    )

    config, _ = ChannelConfig.objects.get_or_create(channel=channel)

    sender_ids = [p.sender_id for p in posts if p.sender_id]
    known_users = {}
    if sender_ids:
        known_users = {
            u.telegram_id: u.pk
            for u in TelegramUser.objects.filter(telegram_id__in=sender_ids)
        }

    # Calculate album positions for posts with grouped_id
    album_info = {}
    grouped_ids = [p.grouped_id for p in posts if p.grouped_id]
    if grouped_ids:
        # Get all messages in these albums, ordered by message_id
        album_messages = ArchivedMessage.objects.filter(
            channel=channel,
            grouped_id__in=grouped_ids
        ).values('grouped_id', 'pk', 'message_id').order_by('grouped_id', 'message_id')

        albums = defaultdict(list)
        for msg in album_messages:
            albums[msg['grouped_id']].append(msg['pk'])

        # Build album_info dict: post.pk -> {'position': N, 'total': M}
        for grouped_id, pks in albums.items():
            total = len(pks)
            for idx, pk in enumerate(pks, 1):
                album_info[pk] = {'position': idx, 'total': total, 'grouped_id': grouped_id, 'channel_pk': channel.pk}

    is_monitored = UserMonitoredSource.objects.filter(user=request.user, channel=channel).exists()
    context = {
        'source': channel,
        'config': config,
        'posts': posts,
        'sort': sort,
        'media_filter': media_filter,
        'per_page': per_page,
        'search': search,
        'pending_message_ids': pending_message_ids,
        'known_users': known_users,
        'album_info': album_info,
        'active_tab': 'posts',
        'is_monitored': is_monitored,
    }
    return render(request, 'audit/source_posts_v3.html', context)


@login_required
@silk_profile(name='audit.feed')
def feed(request):
    """
    Global feed of ALL posts from all sources.
    Similar to monitored_feed but not restricted to pinned sources.
    Includes filters for source and user.
    """
    # Get channels for filter dropdown (only from active accounts)
    channels = TelegramChannel.objects.from_active_accounts().order_by('title')

    # Sort order (default: newest first)
    sort = request.GET.get('sort', 'newest')
    order = '-telegram_date' if sort == 'newest' else 'telegram_date'

    # Filter by media type
    media_filter = request.GET.get('media', '')

    # Filter by specific channel
    channel_filter = request.GET.get('channel', '')

    # Search query
    search = request.GET.get('q', '').strip()

    # Posts per page (user preference, stored in session)
    per_page = request.GET.get('per_page', request.session.get('feed_per_page', 25))
    try:
        per_page = int(per_page)
        per_page = max(10, min(500, per_page))  # Clamp between 10-500
    except (ValueError, TypeError):
        per_page = 25
    request.session['feed_per_page'] = per_page

    # Get all archived messages (from active accounts only)
    # Use channel_id__in to avoid joining through account table on every row
    active_channel_ids = channels.values_list('id', flat=True)
    posts_qs = ArchivedMessage.objects.filter(
        channel_id__in=active_channel_ids
    ).select_related(
        'channel', 'channel__account', 'downloaded_file'
    )

    # Apply channel filter
    if channel_filter:
        try:
            posts_qs = posts_qs.filter(channel_id=int(channel_filter))
        except (ValueError, TypeError):
            pass

    # Apply search filter
    if search:
        q_filter = (
            Q(text__icontains=search) |
            Q(sender_username__icontains=search) |
            Q(sender_name__icontains=search) |
            Q(original_filename__icontains=search) |
            Q(channel__title__icontains=search)
        )
        # If search is numeric, also search by message_id (the ID shown in UI as #1234)
        if search.isdigit():
            q_filter |= Q(message_id=int(search))
        posts_qs = posts_qs.filter(q_filter)

    if media_filter == 'media':
        posts_qs = posts_qs.filter(has_media=True)
    elif media_filter == 'text':
        posts_qs = posts_qs.filter(has_media=False)
    elif media_filter in ['photo', 'video', 'file']:
        posts_qs = posts_qs.filter(media_type=media_filter)

    posts_qs = posts_qs.order_by(order)

    # Pagination (use estimated count when no filters to avoid slow COUNT(*))
    is_filtered = bool(channel_filter or search or media_filter)
    paginator = EstimatedCountPaginator(posts_qs, per_page, is_filtered=is_filtered)
    page = request.GET.get('page', 1)
    posts = paginator.get_page(page)

    # Get message_ids on this page for scoped queries
    page_message_ids = [p.message_id for p in posts]

    # Get pending/downloading status (only for messages on this page)
    pending_message_ids = set(
        DownloadTask.objects.filter(
            message_id__in=page_message_ids,
            status__in=['pending', 'downloading']
        ).values_list('message_id', flat=True)
    ) if page_message_ids else set()

    # Look up which sender_ids exist in our TelegramUser table
    sender_ids = [p.sender_id for p in posts if p.sender_id]
    known_users = {}
    if sender_ids:
        known_users = {
            u.telegram_id: u.pk
            for u in TelegramUser.objects.filter(telegram_id__in=sender_ids)
        }

    # Calculate album positions for posts with grouped_id
    album_info = {}
    grouped_posts = [(p.pk, p.grouped_id, p.channel_id) for p in posts if p.grouped_id]
    if grouped_posts:
        grouped_ids_by_channel = defaultdict(set)
        for _, gid, cid in grouped_posts:
            grouped_ids_by_channel[cid].add(gid)

        # Query all album messages across relevant channels
        album_q = Q()
        for cid, gids in grouped_ids_by_channel.items():
            album_q |= Q(channel_id=cid, grouped_id__in=gids)

        album_messages = ArchivedMessage.objects.filter(album_q).values(
            'channel_id', 'grouped_id', 'pk', 'message_id'
        ).order_by('channel_id', 'grouped_id', 'message_id')

        albums = defaultdict(list)
        for msg in album_messages:
            albums[(msg['channel_id'], msg['grouped_id'])].append(msg['pk'])

        for grouped_id, pks in albums.items():
            total = len(pks)
            for idx, pk in enumerate(pks, 1):
                album_info[pk] = {'position': idx, 'total': total, 'grouped_id': grouped_id[1], 'channel_pk': grouped_id[0]}

    context = {
        'posts': posts,
        'channels': channels,
        'sort': sort,
        'media_filter': media_filter,
        'channel_filter': channel_filter,
        'per_page': per_page,
        'search': search,
        'pending_message_ids': pending_message_ids,
        'known_users': known_users,
        'album_info': album_info,
    }
    return render(request, 'audit/feed.html', context)


@login_required
@silk_profile(name='audit.monitored_feed')
def monitored_feed(request):
    """
    Aggregate feed of posts from all monitored/pinned sources.
    Similar to source_posts but across multiple channels.
    """
    # Get user's monitored channels
    monitored_channels = UserMonitoredSource.objects.filter(
        user=request.user
    ).select_related('channel').values_list('channel_id', flat=True)

    # Get the actual channel objects for display (from active accounts only)
    channels = TelegramChannel.objects.from_active_accounts().filter(pk__in=monitored_channels)

    # Sort order (default: newest first)
    sort = request.GET.get('sort', 'newest')
    order = '-telegram_date' if sort == 'newest' else 'telegram_date'

    # Filter by media type
    media_filter = request.GET.get('media', '')

    # Filter by specific channel
    channel_filter = request.GET.get('channel', '')

    # Search query
    search = request.GET.get('q', '').strip()

    # Posts per page (user preference, stored in session)
    per_page = request.GET.get('per_page', request.session.get('monitored_per_page', 25))
    try:
        per_page = int(per_page)
        per_page = max(10, min(500, per_page))  # Clamp between 10-500
    except (ValueError, TypeError):
        per_page = 25
    request.session['monitored_per_page'] = per_page

    # Get archived messages from all monitored channels (from active accounts only)
    posts_qs = ArchivedMessage.objects.from_active_accounts().filter(
        channel_id__in=monitored_channels
    ).select_related('channel', 'channel__account', 'downloaded_file')

    # Apply channel filter
    if channel_filter:
        try:
            posts_qs = posts_qs.filter(channel_id=int(channel_filter))
        except (ValueError, TypeError):
            pass

    # Apply search filter
    if search:
        q_filter = (
            Q(text__icontains=search) |
            Q(sender_username__icontains=search) |
            Q(sender_name__icontains=search) |
            Q(original_filename__icontains=search) |
            Q(channel__title__icontains=search)
        )
        # If search is numeric, also search by message_id (the ID shown in UI as #1234)
        if search.isdigit():
            q_filter |= Q(message_id=int(search))
        posts_qs = posts_qs.filter(q_filter)

    if media_filter == 'media':
        posts_qs = posts_qs.filter(has_media=True)
    elif media_filter == 'text':
        posts_qs = posts_qs.filter(has_media=False)
    elif media_filter in ['photo', 'video', 'file']:
        posts_qs = posts_qs.filter(media_type=media_filter)

    posts_qs = posts_qs.order_by(order)

    # Pagination
    paginator = Paginator(posts_qs, per_page)
    page = request.GET.get('page', 1)
    posts = paginator.get_page(page)

    # Get message_ids on this page for scoped queries
    page_message_ids = [p.message_id for p in posts]

    # Get pending/downloading status (only for messages on this page)
    pending_message_ids = set(
        DownloadTask.objects.filter(
            message_id__in=page_message_ids,
            status__in=['pending', 'downloading']
        ).values_list('message_id', flat=True)
    ) if page_message_ids else set()

    # Look up which sender_ids exist in our TelegramUser table
    sender_ids = [p.sender_id for p in posts if p.sender_id]
    known_users = {}
    if sender_ids:
        known_users = {
            u.telegram_id: u.pk
            for u in TelegramUser.objects.filter(telegram_id__in=sender_ids)
        }

    # Calculate album positions for posts with grouped_id
    album_info = {}
    grouped_posts = [(p.pk, p.grouped_id, p.channel_id) for p in posts if p.grouped_id]
    if grouped_posts:
        grouped_ids_by_channel = defaultdict(set)
        for _, gid, cid in grouped_posts:
            grouped_ids_by_channel[cid].add(gid)

        album_q = Q()
        for cid, gids in grouped_ids_by_channel.items():
            album_q |= Q(channel_id=cid, grouped_id__in=gids)

        album_messages = ArchivedMessage.objects.filter(album_q).values(
            'channel_id', 'grouped_id', 'pk', 'message_id'
        ).order_by('channel_id', 'grouped_id', 'message_id')

        albums = defaultdict(list)
        for msg in album_messages:
            albums[(msg['channel_id'], msg['grouped_id'])].append(msg['pk'])

        for grouped_id, pks in albums.items():
            total = len(pks)
            for idx, pk in enumerate(pks, 1):
                album_info[pk] = {'position': idx, 'total': total, 'grouped_id': grouped_id[1], 'channel_pk': grouped_id[0]}

    context = {
        'posts': posts,
        'channels': channels,
        'sort': sort,
        'media_filter': media_filter,
        'channel_filter': channel_filter,
        'per_page': per_page,
        'search': search,
        'pending_message_ids': pending_message_ids,
        'known_users': known_users,
        'monitored_count': len(monitored_channels),
        'album_info': album_info,
    }
    return render(request, 'audit/monitored.html', context)


@login_required
def serve_post_thumbnail(request, pk):
    """Serve a thumbnail for an archived message from the storage root."""

    post = get_object_or_404(
        ArchivedMessage.objects.from_active_accounts().select_related('channel__account'),
        pk=pk
    )

    if not post.thumbnail_path:
        raise Http404("No thumbnail available")

    # Determine content type
    content_type, _ = mimetypes.guess_type(post.thumbnail_path)
    if not content_type:
        content_type = 'image/jpeg'

    backend = get_storage_backend()
    return backend.get_serve_response(
        post.thumbnail_path,
        content_type,
    )


@login_required
@require_http_methods(['POST'])
def queue_post_download(request, pk):
    """Queue a specific post's media for download."""

    post = get_object_or_404(
        ArchivedMessage.objects.from_active_accounts().select_related('channel__account', 'channel__config'),
        pk=pk
    )

    if not post.has_media:
        return JsonResponse({'error': 'Post has no media'}, status=400)

    # Check if media is marked as unavailable
    if post.media_unavailable:
        return JsonResponse({
            'error': 'Media unavailable - deleted or edited',
            'unavailable': True
        }, status=400)

    # Check if already downloaded
    if post.downloaded_file:
        return JsonResponse({'error': 'Already downloaded', 'downloaded': True}, status=400)

    # Check if already queued or unavailable
    existing = DownloadTask.objects.filter(
        channel=post.channel,
        message_id=post.message_id
    ).first()

    if existing:
        # Don't allow re-queue if unavailable
        if existing.status == 'unavailable':
            return JsonResponse({
                'error': 'Media unavailable - deleted or edited',
                'unavailable': True
            }, status=400)
        return JsonResponse({
            'status': existing.status,
            'message': f'Already {existing.status}'
        })

    # Create download task
    config = post.channel.config
    effective_priority = config.priority + config.get_file_type_priority(post.media_type)

    # Determine pending reason if channel is paused
    pending_reason = 'channel_paused' if config.is_paused else 'queued'

    task = DownloadTask.objects.create(
        channel=post.channel,
        message_id=post.message_id,
        telegram_file_id=post.telegram_file_id,
        original_filename=post.original_filename,
        file_type=post.media_type,
        file_size=post.file_size,
        mime_type=post.mime_type,
        priority=effective_priority,
        max_retries=3,
        pending_reason=pending_reason,
    )

    # Broadcast queue update via WebSocket for real-time UI updates
    try:
        sync_broadcast_queue_update(request.user.id, 'added', task_id=task.pk)
    except Exception as e:
        pass  # Non-critical, don't fail the request

    return JsonResponse({
        'status': 'queued',
        'message': 'Added to download queue',
        'filename': post.original_filename or post.media_type or 'media'
    })


@login_required
def export_post_json(request, pk):
    """Export a post's metadata as JSON file."""

    post = get_object_or_404(
        ArchivedMessage.objects.from_active_accounts().select_related('channel__account', 'downloaded_file'),
        pk=pk
    )

    # Build comprehensive metadata
    data = {
        'id': post.pk,
        'channel': {
            'id': post.channel.pk,
            'telegram_id': post.channel.telegram_id,
            'title': post.channel.title,
            'username': post.channel.username,
        },
        'message_id': post.message_id,
        'text': post.text,
        'telegram_date': post.telegram_date.isoformat() if post.telegram_date else None,
        'edited_date': post.edited_date.isoformat() if post.edited_date else None,
        'sender': {
            'id': post.sender_id,
            'name': post.sender_name,
            'username': post.sender_username,
        } if post.sender_id else None,
        'media': {
            'has_media': post.has_media,
            'type': post.media_type,
            'telegram_file_id': post.telegram_file_id,
            'original_filename': post.original_filename,
            'file_size': post.file_size,
            'mime_type': post.mime_type,
            'is_unavailable': post.media_unavailable,
        } if post.has_media else None,
        'flags': {
            'is_pinned': post.is_pinned,
            'is_post': post.is_post,
            'noforwards': post.noforwards,
            'is_silent': post.is_silent,
        },
        'engagement': {
            'views': post.views,
            'forwards': post.forwards,
            'reactions': post.reactions,
        },
        'reply_to_message_id': post.reply_to_message_id,
        'grouped_id': post.grouped_id,
        'post_author': post.post_author,
        'ttl_period': post.ttl_period,
        'download_status': {
            'is_downloaded': post.downloaded_file is not None,
            'downloaded_file_id': post.downloaded_file.pk if post.downloaded_file else None,
        },
        'exported_at': timezone.now().isoformat(),
    }

    response = HttpResponse(
        json.dumps(data, indent=2, ensure_ascii=False),
        content_type='application/json'
    )
    response['Content-Disposition'] = f'attachment; filename="post_{post.channel.telegram_id}_{post.message_id}.json"'
    return response


@login_required
@require_http_methods(['POST'])
def report_source(request, pk):
    """Report a channel/group to Telegram."""

    source = get_object_or_404(
        TelegramChannel.objects.from_active_accounts().select_related('account'),
        pk=pk
    )

    reason = request.POST.get('reason')
    report_message = request.POST.get('message', '')

    if not reason:
        messages.error(request, 'Please select a reason for the report')
        return redirect('audit:source_detail', pk=pk)

    # Connect to Telegram and submit report
    service = TelegramService(source.account)

    async def submit_report():
        await service.create_client(
            source.account.api_id,
            source.account.api_hash,
            source.account.phone_number
        )
        try:
            return await service.report_peer(source.telegram_id, reason, report_message, username=source.username)
        finally:
            await service.disconnect()

    result = run_async(submit_report())

    # Record the report
    TelegramReport.objects.create(
        report_type='channel',
        reason=reason,
        message=report_message,
        channel=source,
        reported_telegram_id=source.telegram_id,
        reported_name=source.title,
        account=source.account,
        success=result['success'],
        error_message=result.get('error', ''),
    )

    if result['success']:
        messages.success(request, f'Reported {source.title} to Telegram')
    else:
        messages.error(request, f'Failed to report: {result["error"]}')

    return redirect('audit:source_detail', pk=pk)


@login_required
@require_http_methods(['POST'])
def leave_source(request, pk):
    """Leave a channel/group on Telegram and deactivate it in Trawlr."""

    source = get_object_or_404(
        TelegramChannel.objects.from_active_accounts().select_related('account', 'config'),
        pk=pk
    )

    # Connect to Telegram and leave
    service = TelegramService(source.account)

    async def do_leave():
        await service.create_client(
            source.account.api_id,
            source.account.api_hash,
            source.account.phone_number
        )
        try:
            return await service.leave_channel(source.telegram_id, username=source.username)
        finally:
            await service.disconnect()

    result = run_async(do_leave())

    if result['success']:
        # Update source: mark as left and inactive
        source.has_left = True
        source.active = False
        source.save(update_fields=['has_left', 'active'])

        # Disable auto-download
        if hasattr(source, 'config') and source.config:
            source.config.auto_download_enabled = False
            source.config.save(update_fields=['auto_download_enabled'])

        messages.success(request, f'Left {source.title} on Telegram. Source deactivated.')
    else:
        messages.error(request, f'Failed to leave: {result["error"]}')

    return redirect('audit:source_detail', pk=pk)


@login_required
@require_http_methods(['POST'])
def report_user(request, pk):
    """Report a user to Telegram."""

    telegram_user = get_object_or_404(TelegramUser, pk=pk)

    # Get account from first channel membership that belongs to this user
    user_channels = TelegramChannel.objects.from_active_accounts()
    membership = UserGroupMembership.objects.filter(
        user=telegram_user,
        channel__in=user_channels
    ).select_related('channel__account').first()

    if not membership:
        messages.error(request, 'Cannot report: no associated account found')
        return redirect('audit:user_detail', pk=pk)

    reason = request.POST.get('reason')
    report_message = request.POST.get('message', '')

    if not reason:
        messages.error(request, 'Please select a reason for the report')
        return redirect('audit:user_detail', pk=pk)

    account = membership.channel.account
    service = TelegramService(account)

    async def submit_report():
        await service.create_client(
            account.api_id,
            account.api_hash,
            account.phone_number
        )
        try:
            return await service.report_peer(telegram_user.telegram_id, reason, report_message, username=telegram_user.username)
        finally:
            await service.disconnect()

    result = run_async(submit_report())

    # Record the report
    TelegramReport.objects.create(
        report_type='user',
        reason=reason,
        message=report_message,
        user=telegram_user,
        reported_telegram_id=telegram_user.telegram_id,
        reported_name=telegram_user.display_name,
        account=account,
        success=result['success'],
        error_message=result.get('error', ''),
    )

    if result['success']:
        messages.success(request, f'Reported {telegram_user.display_name} to Telegram')
        # Mark user as reported
        telegram_user.reported_to_telegram = True
        telegram_user.reported_at = timezone.now()
        telegram_user.save(update_fields=['reported_to_telegram', 'reported_at'])
    else:
        messages.error(request, f'Failed to report: {result["error"]}')

    return redirect('audit:user_detail', pk=pk)


@login_required
@require_http_methods(['POST'])
def report_post(request, source_pk, message_id):
    """Report a specific message to Telegram."""

    source = get_object_or_404(
        TelegramChannel.objects.from_active_accounts().select_related('account'),
        pk=source_pk
    )

    reason = request.POST.get('reason')
    report_message = request.POST.get('message', '')

    if not reason:
        messages.error(request, 'Please select a reason for the report')
        return redirect('audit:source_posts', pk=source_pk)

    # Try to find the archived message
    archived_msg = ArchivedMessage.objects.filter(
        channel=source,
        message_id=message_id
    ).first()

    service = TelegramService(source.account)

    async def submit_report():
        await service.create_client(
            source.account.api_id,
            source.account.api_hash,
            source.account.phone_number
        )
        try:
            return await service.report_messages(
                source.telegram_id,
                [message_id],
                reason,
                report_message,
                username=source.username
            )
        finally:
            await service.disconnect()

    result = run_async(submit_report())

    # Record the report
    TelegramReport.objects.create(
        report_type='message',
        reason=reason,
        message=report_message,
        channel=source,
        archived_message=archived_msg,
        reported_telegram_id=source.telegram_id,
        reported_message_id=message_id,
        reported_name=f'{source.title} #{message_id}',
        account=source.account,
        success=result['success'],
        error_message=result.get('error', ''),
    )

    if result['success']:
        messages.success(request, 'Reported message to Telegram')
    else:
        messages.error(request, f'Failed to report: {result["error"]}')

    return redirect('audit:source_posts', pk=source_pk)


@login_required
@require_http_methods(['POST'])
def flag_user(request, pk):
    """Flag a user for review (without reporting to Telegram yet)."""

    telegram_user = get_object_or_404(TelegramUser, pk=pk)

    # Verify access
    user_channels = TelegramChannel.objects.from_active_accounts()
    if not UserGroupMembership.objects.filter(user=telegram_user, channel__in=user_channels).exists():
        raise Http404("User not found")

    reason = request.POST.get('reason', '')
    notes = request.POST.get('notes', '')

    telegram_user.is_flagged = True
    telegram_user.flagged_reason = reason
    telegram_user.flagged_notes = notes
    telegram_user.flagged_at = timezone.now()
    telegram_user.save(update_fields=['is_flagged', 'flagged_reason', 'flagged_notes', 'flagged_at'])

    messages.success(request, f'Flagged {telegram_user.display_name} for review')

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'status': 'flagged', 'reason': reason})

    return redirect('audit:user_detail', pk=pk)


@login_required
@require_http_methods(['POST'])
def unflag_user(request, pk):
    """Remove flag from a user."""

    telegram_user = get_object_or_404(TelegramUser, pk=pk)

    # Verify access
    user_channels = TelegramChannel.objects.from_active_accounts()
    if not UserGroupMembership.objects.filter(user=telegram_user, channel__in=user_channels).exists():
        raise Http404("User not found")

    telegram_user.is_flagged = False
    telegram_user.flagged_reason = ''
    telegram_user.flagged_notes = ''
    telegram_user.flagged_at = None
    telegram_user.save(update_fields=['is_flagged', 'flagged_reason', 'flagged_notes', 'flagged_at'])

    messages.success(request, f'Removed flag from {telegram_user.display_name}')

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'status': 'unflagged'})

    return redirect('audit:user_detail', pk=pk)


@login_required
def user_profile_photo(request, pk):
    """Return base64 profile photo data for a user."""

    telegram_user = get_object_or_404(TelegramUser, pk=pk)

    # Verify access
    user_channels = TelegramChannel.objects.from_active_accounts()
    if not UserGroupMembership.objects.filter(user=telegram_user, channel__in=user_channels).exists():
        raise Http404("User not found")

    if not telegram_user.profile_photo_base64:
        return JsonResponse({'has_photo': False})

    return JsonResponse({
        'has_photo': True,
        'photo_base64': telegram_user.profile_photo_base64,
        'updated_at': telegram_user.profile_photo_updated_at.isoformat() if telegram_user.profile_photo_updated_at else None,
    })


@login_required
@require_http_methods(['POST'])
def fetch_user_profile(request, pk):
    """Queue a task to fetch full profile for a single user."""

    telegram_user = get_object_or_404(TelegramUser, pk=pk)

    user_channels = TelegramChannel.objects.from_active_accounts()
    membership = UserGroupMembership.objects.filter(
        user=telegram_user,
        channel__in=user_channels
    ).select_related('channel__account').first()

    if not membership:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'No associated account found'}, status=400)
        messages.error(request, 'Cannot fetch profile: no associated account found')
        return redirect('audit:user_detail', pk=pk)

    if not telegram_user.access_hash:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'User has no access hash'}, status=400)
        messages.error(request, 'Cannot fetch profile: user has no access hash')
        return redirect('audit:user_detail', pk=pk)

    # Queue the task
    fetch_single_user_profile.send(telegram_user.pk, membership.channel.pk)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'status': 'started',
            'message': f'Profile fetch queued for {telegram_user.display_name}'
        })

    messages.success(request, f'Profile fetch queued for {telegram_user.display_name}')
    return redirect('audit:user_detail', pk=pk)


@login_required
@require_http_methods(["POST"])
def delete_user_media(request, pk):
    """Queue a background task to delete all media for a specific user."""
    from tasks import delete_user_media as delete_user_media_task

    telegram_user = get_object_or_404(TelegramUser, pk=pk)

    # Note is mandatory
    note_text = request.POST.get('note', '').strip()
    if not note_text:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': 'A note is required'}, status=400)
        messages.error(request, 'A note is required when deleting media')
        return redirect('audit:user_detail', pk=pk)

    # Create the user note
    UserNote.add_note(
        telegram_user,
        f'[Media Deletion] {note_text}',
        request.user,
    )

    task_id = str(uuid.uuid4())
    task_run = TaskRun.create_task('delete_user_media', task_id)

    delete_user_media_task.send_with_options(
        args=(telegram_user.pk,),
        kwargs={'task_run_id': task_id},
        priority=0,
    )

    result_msg = f'Media deletion queued for {telegram_user.display_name}'
    logger.info(f"delete_user_media: {result_msg}")

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'message': result_msg,
        })

    messages.success(request, result_msg)
    return redirect('audit:user_detail', pk=pk)


@login_required
@silk_profile(name='audit.activity')
def activity(request):
    """
    Activity dashboard showing real-time system activity.
    Shows recent activity logs with auto-refresh via HTMX.
    """

    # Get filter parameters
    search = request.GET.get('q', '')
    activity_type = request.GET.get('type', '')
    source = request.GET.get('source', '')
    channel_id = request.GET.get('channel', '')
    date_range = request.GET.get('date', '24h')  # Default to last 24 hours

    # Calculate date filter
    date_filter_map = {
        '1h': timedelta(hours=1),
        '6h': timedelta(hours=6),
        '24h': timedelta(hours=24),
        '7d': timedelta(days=7),
        '30d': timedelta(days=30),
        'all': None,
    }
    date_delta = date_filter_map.get(date_range, timedelta(hours=24))

    # Build queryset with filters - only select_related channel (telegram_user not used in list)
    activities = ActivityLog.objects.select_related('channel').order_by('-timestamp')

    # Apply date filter
    if date_delta:
        cutoff = timezone.now() - date_delta
        activities = activities.filter(timestamp__gte=cutoff)

    if search:
        activities = activities.filter(description__icontains=search)

    if activity_type:
        activities = activities.filter(activity_type=activity_type)

    if source:
        activities = activities.filter(source=source)

    if channel_id:
        activities = activities.filter(channel_id=channel_id)

    # Pagination
    paginator = Paginator(activities, 50)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    # Group by activity type for summary

    # Get counts by type for last hour
    one_hour_ago = timezone.now() - timezone.timedelta(hours=1)
    type_counts_raw = ActivityLog.objects.filter(
        timestamp__gte=one_hour_ago
    ).values('activity_type').annotate(count=Count('id')).order_by('-count')

    # Add display names to type counts
    activity_type_labels = dict(ActivityLog.ACTIVITY_TYPE_CHOICES)
    type_counts = [
        {
            'activity_type': item['activity_type'],
            'count': item['count'],
            'display_name': activity_type_labels.get(item['activity_type'], item['activity_type'].replace('_', ' ').title()),
        }
        for item in type_counts_raw
    ]

    # Get source distribution for last hour
    source_counts = ActivityLog.objects.filter(
        timestamp__gte=one_hour_ago
    ).values('source').annotate(count=Count('id')).order_by('-count')

    # Get distinct activity types and sources for filter dropdowns
    activity_types = ActivityLog.ACTIVITY_TYPE_CHOICES
    sources = ActivityLog.SOURCE_CHOICES

    # Get channels that have recent activity (within date range) - much faster than checking all activity
    if date_delta:
        channel_ids_with_activity = ActivityLog.objects.filter(
            timestamp__gte=cutoff,
            channel__isnull=False
        ).values_list('channel_id', flat=True).distinct()[:100]
    else:
        # For "all time", just get channels from recent activity to avoid slow query
        recent_cutoff = timezone.now() - timedelta(days=7)
        channel_ids_with_activity = ActivityLog.objects.filter(
            timestamp__gte=recent_cutoff,
            channel__isnull=False
        ).values_list('channel_id', flat=True).distinct()[:100]

    channels_with_activity = TelegramChannel.objects.from_active_accounts().filter(
        pk__in=list(channel_ids_with_activity)
    ).order_by('title')

    # RabbitMQ queue stats disabled for now
    queue_stats = None

    # Check if this is an HTMX request for just the activity list
    if request.headers.get('HX-Request'):
        return render(request, 'audit/partials/activity_list.html', {
            'page_obj': page_obj,
            'search': search,
            'activity_type': activity_type,
            'source': source,
            'channel_id': channel_id,
            'date_range': date_range,
        })

    return render(request, 'audit/activity.html', {
        'page_obj': page_obj,
        'type_counts': type_counts,
        'source_counts': source_counts,
        'queue_stats': queue_stats,
        'search': search,
        'activity_type': activity_type,
        'source': source,
        'channel_id': channel_id,
        'date_range': date_range,
        'activity_types': activity_types,
        'sources': sources,
        'channels_with_activity': channels_with_activity,
    })


@login_required
@silk_profile(name='audit.tasks')
def tasks(request):
    """
    Task management view showing running/queued background tasks.
    Allows viewing progress and cancelling tasks.
    """

    # Get filter parameters
    status = request.GET.get('status', '')
    task_type = request.GET.get('type', '')
    date_range = request.GET.get('date', '24h')  # Default to last 24 hours

    # Calculate date filter
    date_filter_map = {
        '1h': timedelta(hours=1),
        '6h': timedelta(hours=6),
        '24h': timedelta(hours=24),
        '7d': timedelta(days=7),
        '30d': timedelta(days=30),
        'all': None,
    }
    date_delta = date_filter_map.get(date_range, timedelta(hours=24))

    # Build queryset - running first, then queued, then rest by created_at desc
    task_runs = TaskRun.objects.select_related('channel', 'account').annotate(
        status_order=Case(
            When(status='running', then=Value(0)),
            When(status='queued', then=Value(1)),
            default=Value(2),
        )
    ).order_by('status_order', '-created_at')

    # Apply date filter
    if date_delta:
        cutoff = timezone.now() - date_delta
        task_runs = task_runs.filter(created_at__gte=cutoff)

    if status:
        task_runs = task_runs.filter(status=status)

    if task_type:
        task_runs = task_runs.filter(task_type=task_type)

    # Get counts by status (within date range)
    status_qs = TaskRun.objects.all()
    if date_delta:
        status_qs = status_qs.filter(created_at__gte=cutoff)
    status_counts = status_qs.values('status').annotate(count=Count('id'))
    status_dict = {item['status']: item['count'] for item in status_counts}

    # Pagination
    paginator = Paginator(task_runs, 50)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    # Check if this is an HTMX request for just the task list
    if request.headers.get('HX-Request'):
        return render(request, 'audit/partials/task_list.html', {
            'page_obj': page_obj,
            'status': status,
            'task_type': task_type,
            'date_range': date_range,
        })

    return render(request, 'audit/tasks.html', {
        'page_obj': page_obj,
        'status': status,
        'task_type': task_type,
        'date_range': date_range,
        'status_counts': status_dict,
        'task_types': TaskRun.TASK_TYPE_CHOICES,
        'status_choices': TaskRun.STATUS_CHOICES,
    })


@login_required
@require_http_methods(['POST'])
def cancel_task(request, pk):
    """Cancel a running task."""

    task_run = get_object_or_404(TaskRun, pk=pk)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    force = request.GET.get('force') == '1'

    # Only allow cancelling running or queued tasks
    if task_run.status not in ['queued', 'running']:
        if is_ajax:
            return JsonResponse({
                'error': f'Cannot cancel task with status: {task_run.status}'
            }, status=400)
        messages.error(request, f'Cannot cancel task with status: {task_run.status}')
        return redirect('audit:tasks')

    if force:
        # Force cancel - immediately mark as cancelled
        task_run.mark_cancelled()
        if is_ajax:
            return JsonResponse({
                'status': 'cancelled',
                'message': 'Task force cancelled.'
            })
        messages.success(request, 'Task force cancelled.')
    else:
        # Normal cancel - request cooperative cancellation
        task_run.request_cancel()
        if is_ajax:
            return JsonResponse({
                'status': 'cancelling',
                'message': 'Cancellation requested. Task will stop at next checkpoint.'
            })
        messages.success(request, 'Cancellation requested. Task will stop at next checkpoint.')

    return redirect('audit:tasks')


@login_required
@require_http_methods(['POST'])
def cleanup_tasks(request):
    """Clean up old completed/failed/cancelled tasks."""

    # Delete tasks older than 7 days
    cutoff = timezone.now() - timedelta(hours=7)
    deleted, _ = TaskRun.objects.filter(
        status__in=['completed', 'failed', 'cancelled'],
        completed_at__lt=cutoff
    ).delete()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'status': 'success',
            'message': f'Cleaned up {deleted} old tasks'
        })

    messages.success(request, f'Cleaned up {deleted} old tasks')
    return redirect('audit:tasks')


@login_required
def source_tasks(request, pk):
    """HTMX partial: Task execution history for a source."""

    channel = get_object_or_404(
        TelegramChannel,
        pk=pk
    )

    # Get task runs for this channel, ordered by most recent first
    task_runs = TaskRun.objects.filter(
        channel=channel
    ).order_by('-created_at')

    # Pagination
    per_page = request.GET.get('tasks_per_page', request.session.get('source_tasks_per_page', 5))
    try:
        per_page = int(per_page)
        per_page = max(5, min(50, per_page))
    except (ValueError, TypeError):
        per_page = 5
    request.session['source_tasks_per_page'] = per_page

    paginator = Paginator(task_runs, per_page)
    page_number = request.GET.get('tasks_page', 1)
    page_obj = paginator.get_page(page_number)

    return render(request, 'audit/partials/source_tasks.html', {
        'source': channel,
        'page_obj': page_obj,
        'tasks_per_page': per_page,
    })


# =============================================================================
# Notes Views (Sources and Users)
# =============================================================================

@login_required
def source_notes(request, pk):
    """View and manage notes for a source."""

    channel = get_object_or_404(
        TelegramChannel.objects.from_active_accounts().select_related('account', 'config'),
        pk=pk
    )

    config, _ = ChannelConfig.objects.get_or_create(channel=channel)
    notes = UserNote.get_for_object(channel)

    # Get file counts for nav badge
    file_counts_query = DownloadedFile.objects.filter(channel=channel).values('file_type').annotate(count=Count('id'))
    file_counts = {'photos': 0, 'videos': 0, 'files': 0, 'total': 0}
    for item in file_counts_query:
        if item['file_type'] == 'photo':
            file_counts['photos'] = item['count']
        elif item['file_type'] == 'video':
            file_counts['videos'] = item['count']
        elif item['file_type'] == 'file':
            file_counts['files'] = item['count']
        file_counts['total'] += item['count']

    is_monitored = UserMonitoredSource.objects.filter(user=request.user, channel=channel).exists()

    return render(request, 'audit/source_notes.html', {
        'source': channel,
        'config': config,
        'notes': notes,
        'notes_count': notes.count(),
        'file_counts': file_counts,
        'active_tab': 'notes',
        'is_monitored': is_monitored,
    })


@login_required
def source_tags(request, pk):
    """View and manage tags for a source."""
    channel = get_object_or_404(
        TelegramChannel.objects.from_active_accounts().select_related('account', 'config'),
        pk=pk
    )
    config, _ = ChannelConfig.objects.get_or_create(channel=channel)
    all_tags = Tag.objects.all()
    notes_count = UserNote.get_for_object(channel).count()

    file_counts_query = DownloadedFile.objects.filter(channel=channel).values('file_type').annotate(count=Count('id'))
    file_counts = {'photos': 0, 'videos': 0, 'files': 0, 'total': 0}
    for item in file_counts_query:
        if item['file_type'] == 'photo':
            file_counts['photos'] = item['count']
        elif item['file_type'] == 'video':
            file_counts['videos'] = item['count']
        elif item['file_type'] == 'file':
            file_counts['files'] = item['count']
        file_counts['total'] += item['count']

    is_monitored = UserMonitoredSource.objects.filter(user=request.user, channel=channel).exists()

    return render(request, 'audit/source_tags.html', {
        'source': channel,
        'config': config,
        'all_tags': all_tags,
        'colour_choices': TAG_COLOUR_CHOICES,
        'notes_count': notes_count,
        'tag_count': channel.tags.count(),
        'file_counts': file_counts,
        'active_tab': 'tags',
        'is_monitored': is_monitored,
    })


@login_required
@require_http_methods(['POST'])
def source_notes_add(request, pk):
    """Add a note to a source."""
    channel = get_object_or_404(
        TelegramChannel,
        pk=pk
    )

    text = request.POST.get('text', '').strip()
    if not text:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Note text is required'}, status=400)
        messages.error(request, 'Note text is required.')
        return redirect('audit:source_notes', pk=pk)

    UserNote.add_note(channel, text, request.user)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'message': 'Note added successfully'})

    messages.success(request, 'Note added.')
    return redirect('audit:source_notes', pk=pk)


@login_required
@require_http_methods(['POST'])
def source_notes_edit(request, pk, note_pk):
    """Edit a note on a source."""
    channel = get_object_or_404(
        TelegramChannel,
        pk=pk
    )

    note = get_object_or_404(UserNote, pk=note_pk)

    # Verify the note belongs to this channel
    channel_ct = ContentType.objects.get_for_model(channel)
    if note.content_type != channel_ct or note.object_id != channel.pk:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Note not found'}, status=404)
        raise Http404('Note not found')

    text = request.POST.get('text', '').strip()
    if not text:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Note text is required'}, status=400)
        messages.error(request, 'Note text is required.')
        return redirect('audit:source_notes', pk=pk)

    note.text = text
    note.save()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'message': 'Note updated successfully'})

    messages.success(request, 'Note updated.')
    return redirect('audit:source_notes', pk=pk)


@login_required
@require_http_methods(['POST'])
def source_notes_delete(request, pk, note_pk):
    """Delete a note from a source."""
    channel = get_object_or_404(
        TelegramChannel,
        pk=pk
    )

    note = get_object_or_404(UserNote, pk=note_pk)

    # Verify the note belongs to this channel
    channel_ct = ContentType.objects.get_for_model(channel)
    if note.content_type != channel_ct or note.object_id != channel.pk:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Note not found'}, status=404)
        raise Http404('Note not found')

    note.delete()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'message': 'Note deleted'})

    messages.success(request, 'Note deleted.')
    return redirect('audit:source_notes', pk=pk)


@login_required
def user_notes(request, pk):
    """View and manage notes for a user."""

    user = get_object_or_404(TelegramUser, pk=pk)

    # Verify the requesting user has access (the TelegramUser has been seen by their accounts)
    user_account_ids = list(
        request.user.telegram_accounts.values_list('pk', flat=True)
    )
    has_access = UserGroupMembership.objects.filter(
        user=user,
        channel__account_id__in=user_account_ids
    ).exists()

    if not has_access:
        raise Http404('User not found')

    notes = UserNote.get_for_object(user)

    # Get user's group memberships for context
    memberships = UserGroupMembership.objects.filter(
        user=user,
        channel__account_id__in=user_account_ids
    ).select_related('channel').order_by('-last_seen')[:10]

    exclusions = ExclusionRule.objects.filter(telegram_user=user)
    has_global_exclusion = exclusions.filter(is_global=True).exists()
    source_exclusion_count = exclusions.filter(is_global=False).count()

    return render(request, 'audit/user_notes.html', {
        'telegram_user': user,
        'notes': notes,
        'notes_count': notes.count(),
        'tag_count': user.tags.count(),
        'memberships': memberships,
        'has_global_exclusion': has_global_exclusion,
        'source_exclusion_count': source_exclusion_count,
        'active_tab': 'notes',
    })


@login_required
@require_http_methods(['POST'])
def user_notes_add(request, pk):
    """Add a note to a user."""

    user = get_object_or_404(TelegramUser, pk=pk)

    # Verify access
    user_account_ids = list(
        request.user.telegram_accounts.values_list('pk', flat=True)
    )
    has_access = UserGroupMembership.objects.filter(
        user=user,
        channel__account_id__in=user_account_ids
    ).exists()

    if not has_access:
        raise Http404('User not found')

    text = request.POST.get('text', '').strip()
    if not text:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Note text is required'}, status=400)
        messages.error(request, 'Note text is required.')
        return redirect('audit:user_notes', pk=pk)

    UserNote.add_note(user, text, request.user)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'message': 'Note added successfully'})

    messages.success(request, 'Note added.')
    return redirect('audit:user_notes', pk=pk)


@login_required
@require_http_methods(['POST'])
def user_notes_edit(request, pk, note_pk):
    """Edit a note on a user."""

    user = get_object_or_404(TelegramUser, pk=pk)

    # Verify access
    user_account_ids = list(
        request.user.telegram_accounts.values_list('pk', flat=True)
    )
    has_access = UserGroupMembership.objects.filter(
        user=user,
        channel__account_id__in=user_account_ids
    ).exists()

    if not has_access:
        raise Http404('User not found')

    note = get_object_or_404(UserNote, pk=note_pk)

    # Verify the note belongs to this user
    user_ct = ContentType.objects.get_for_model(user)
    if note.content_type != user_ct or note.object_id != user.pk:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Note not found'}, status=404)
        raise Http404('Note not found')

    text = request.POST.get('text', '').strip()
    if not text:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Note text is required'}, status=400)
        messages.error(request, 'Note text is required.')
        return redirect('audit:user_notes', pk=pk)

    note.text = text
    note.save()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'message': 'Note updated successfully'})

    messages.success(request, 'Note updated.')
    return redirect('audit:user_notes', pk=pk)


@login_required
@require_http_methods(['POST'])
def user_notes_delete(request, pk, note_pk):
    """Delete a note from a user."""

    user = get_object_or_404(TelegramUser, pk=pk)

    # Verify access
    user_account_ids = list(
        request.user.telegram_accounts.values_list('pk', flat=True)
    )
    has_access = UserGroupMembership.objects.filter(
        user=user,
        channel__account_id__in=user_account_ids
    ).exists()

    if not has_access:
        raise Http404('User not found')

    note = get_object_or_404(UserNote, pk=note_pk)

    # Verify the note belongs to this user
    user_ct = ContentType.objects.get_for_model(user)
    if note.content_type != user_ct or note.object_id != user.pk:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Note not found'}, status=404)
        raise Http404('Note not found')

    note.delete()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'message': 'Note deleted'})

    messages.success(request, 'Note deleted.')
    return redirect('audit:user_notes', pk=pk)


@login_required
def post_history(request, pk):
    """HTMX partial: show edit history for a message via django-simple-history."""
    post = get_object_or_404(
        ArchivedMessage.objects.from_active_accounts(),
        pk=pk
    )

    history = (
        post.history
        .filter(text__isnull=False)
        .exclude(text='')
        .order_by('-history_date')
        .values('text', 'history_date')
    )

    return render(request, 'audit/partials/post_history.html', {
        'history': history,
    })


@login_required
@require_http_methods(["POST"])
def source_add_tag(request, pk):
    """Add a tag to a source."""
    source = get_object_or_404(TelegramChannel.objects.from_active_accounts(), pk=pk)
    tag_id = request.POST.get('tag_id')
    tag_name = request.POST.get('tag_name', '').strip()

    colour = request.POST.get('colour', 'blue')

    if tag_id:
        tag = get_object_or_404(Tag, pk=tag_id)
    elif tag_name:
        tag, created = Tag.objects.get_or_create(name=tag_name, defaults={'colour': colour})
    else:
        return JsonResponse({'error': 'tag_id or tag_name required'}, status=400)

    source.tags.add(tag)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'status': 'ok', 'tag': {'id': tag.id, 'name': tag.name, 'colour': tag.colour}})
    return redirect('audit:source_detail', pk=pk)


@login_required
@require_http_methods(["POST"])
def source_remove_tag(request, pk, tag_pk):
    """Remove a tag from a source."""
    source = get_object_or_404(TelegramChannel.objects.from_active_accounts(), pk=pk)
    source.tags.remove(tag_pk)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'status': 'ok'})
    return redirect('audit:source_detail', pk=pk)


@login_required
def user_tags(request, pk):
    """View and manage tags for a user."""
    telegram_user = get_object_or_404(
        TelegramUser.objects.prefetch_related('tags'),
        pk=pk,
    )

    # Verify access
    user_channels = TelegramChannel.objects.from_active_accounts().all()
    if not UserGroupMembership.objects.filter(user=telegram_user, channel__in=user_channels).exists():
        raise Http404("User not found")

    all_tags = Tag.objects.all()
    notes_count = UserNote.get_for_object(telegram_user).count()

    exclusions = ExclusionRule.objects.filter(telegram_user=telegram_user)
    has_global_exclusion = exclusions.filter(is_global=True).exists()
    source_exclusion_count = exclusions.filter(is_global=False).count()

    return render(request, 'audit/user_tags.html', {
        'telegram_user': telegram_user,
        'all_tags': all_tags,
        'colour_choices': TAG_COLOUR_CHOICES,
        'notes_count': notes_count,
        'tag_count': telegram_user.tags.count(),
        'has_global_exclusion': has_global_exclusion,
        'source_exclusion_count': source_exclusion_count,
        'active_tab': 'tags',
    })


@login_required
@require_http_methods(["POST"])
def user_add_tag(request, pk):
    """Add a tag to a user."""
    telegram_user = get_object_or_404(TelegramUser, pk=pk)
    tag_id = request.POST.get('tag_id')
    tag_name = request.POST.get('tag_name', '').strip()
    colour = request.POST.get('colour', 'blue')

    if tag_id:
        tag = get_object_or_404(Tag, pk=tag_id)
    elif tag_name:
        tag, created = Tag.objects.get_or_create(name=tag_name, defaults={'colour': colour})
    else:
        return JsonResponse({'error': 'tag_id or tag_name required'}, status=400)

    telegram_user.tags.add(tag)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'status': 'ok', 'tag': {'id': tag.id, 'name': tag.name, 'colour': tag.colour}})
    return redirect('audit:user_detail', pk=pk)


@login_required
@require_http_methods(["POST"])
def user_remove_tag(request, pk, tag_pk):
    """Remove a tag from a user."""
    telegram_user = get_object_or_404(TelegramUser, pk=pk)
    telegram_user.tags.remove(tag_pk)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'status': 'ok'})
    return redirect('audit:user_detail', pk=pk)
