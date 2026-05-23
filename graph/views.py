"""
Map visualization views and API endpoints.
Each map type shows a different relationship network.
"""

import logging
from collections import defaultdict
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from silk.profiling.profiler import silk_profile
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from audit.models import (
    TelegramChannel,
    TelegramUser,
    UserGroupMembership,
    ForwardSource,
    MessageEntity,
)
from downloads.models import ArchivedMessage, DownloadedFile
from reports.queries_activity import get_aggregate_filter_options

logger = logging.getLogger('trawlr.graph')


# =============================================================================
# Page Views
# =============================================================================

@login_required
def index(request):
    """Map index - overview of all available map types."""
    # Get some stats for the overview cards
    user_channels = TelegramChannel.objects.filter(
        active=True
    )
    channel_count = user_channels.count()

    user_count = UserGroupMembership.objects.filter(
        channel__in=user_channels,
        active=True
    ).values('user_id').distinct().count()

    forward_count = ForwardSource.objects.filter(
        message__channel__in=user_channels
    ).count()

    return render(request, 'graph/index.html', {
        'channel_count': channel_count,
        'user_count': user_count,
        'forward_count': forward_count,
    })


@login_required
def map_forwards(request):
    """Forward network map - shows content forwarding between channels."""
    channels = TelegramChannel.objects.filter(
        active=True
    ).order_by('title')

    return render(request, 'graph/map_forwards.html', {
        'channels': channels,
        'map_title': 'Forward Network',
        'map_description': 'Shows which channels forward content from which sources',
    })


@login_required
def map_users(request):
    """User network map - shows users and their channel memberships."""
    channels = TelegramChannel.objects.filter(
        active=True
    ).order_by('title')

    return render(request, 'graph/map_users.html', {
        'channels': channels,
        'map_title': 'User Network',
        'map_description': 'Shows users and which channels they belong to',
    })


@login_required
def map_crosspost(request):
    """Cross-post map - shows users who post in multiple channels."""
    channels = TelegramChannel.objects.filter(
        active=True
    ).order_by('title')

    return render(request, 'graph/map_crosspost.html', {
        'channels': channels,
        'map_title': 'Cross-Post Network',
        'map_description': 'Shows users who post messages across multiple channels',
    })


@login_required
def map_urls(request):
    """URL sharing map - shows URL sharing patterns across channels."""
    channels = TelegramChannel.objects.filter(
        active=True
    ).order_by('title')

    return render(request, 'graph/map_urls.html', {
        'channels': channels,
        'map_title': 'URL Sharing Network',
        'map_description': 'Shows which URLs/domains are shared across channels',
    })


@login_required
def map_media(request):
    """Shared media map - shows channels sharing identical media files."""
    channels = TelegramChannel.objects.filter(
        active=True
    ).order_by('title')

    return render(request, 'graph/map_media.html', {
        'channels': channels,
        'map_title': 'Shared Media Network',
        'map_description': 'Shows channels that share identical media files',
    })


@login_required
def map_mentions(request):
    """Mention network map - shows who mentions whom."""
    channels = TelegramChannel.objects.filter(
        active=True
    ).order_by('title')

    return render(request, 'graph/map_mentions.html', {
        'channels': channels,
        'map_title': 'Mention Network',
        'map_description': 'Shows @mention relationships between users',
    })


@login_required
def map_admins(request):
    """Admin network map - shows admin relationships across channels."""
    channels = TelegramChannel.objects.filter(
        active=True
    ).order_by('title')

    return render(request, 'graph/map_admins.html', {
        'channels': channels,
        'map_title': 'Admin Network',
        'map_description': 'Shows which users are admins of which channels',
    })


@login_required
def map_activity(request):
    """Activity Map - aggregate temporal pattern across all active sources."""
    filter_options = get_aggregate_filter_options()
    return render(request, 'graph/map_activity.html', {
        'map_title': 'Activity Map',
        'map_description': 'Temporal pattern of posting across all active sources. Filter by tag or channel type.',
        'filter_tags': filter_options['tags'],
        'filter_channel_types': filter_options['channel_types'],
    })


@login_required
def map_cib(request):
    """Coordinated Inauthentic Behavior detector - clusters of identical content posted across channels."""
    return render(request, 'cib/page.html', {
        'map_title': 'Coordinated Inauthentic Behavior',
        'map_description': (
            'Detects clusters of channels posting identical content (text, media, or URLs) '
            'within a tight time window — the statistical signature of orchestrated campaigns.'
        ),
    })


# =============================================================================
# API Endpoints
# =============================================================================

@login_required
@silk_profile(name='graph.api_forward_network')
def api_forward_network(request):
    """
    API: Forward network graph.
    Shows which channels forward content from which sources.
    """
    user_channel_ids = list(TelegramChannel.objects.filter(
        active=True
    ).values_list('id', flat=True))

    forward_data = ForwardSource.objects.filter(
        message__channel_id__in=user_channel_ids,
        source_telegram_id__isnull=False
    ).values(
        'message__channel_id',
        'message__channel__title',
        'message__channel__telegram_id',
        'source_telegram_id',
        'source_title',
        'source_type'
    ).annotate(
        weight=Count('id')
    ).order_by('-weight')[:500]

    nodes = {}
    edges = []

    for fwd in forward_data:
        # Target node (our channel)
        target_key = f"c_{fwd['message__channel_id']}"
        if target_key not in nodes:
            nodes[target_key] = {
                'id': target_key,
                'label': fwd['message__channel__title'] or f"Channel {fwd['message__channel__telegram_id']}",
                'type': 'channel',
                'telegram_id': fwd['message__channel__telegram_id'],
                'group': 'owned',
            }

        # Source node
        source_key = f"{fwd['source_type'][0]}_{fwd['source_telegram_id']}"
        if source_key not in nodes:
            nodes[source_key] = {
                'id': source_key,
                'label': fwd['source_title'] or f"Unknown {fwd['source_telegram_id']}",
                'type': fwd['source_type'],
                'telegram_id': fwd['source_telegram_id'],
                'group': 'external',
            }

        edges.append({
            'from': source_key,
            'to': target_key,
            'value': fwd['weight'],
            'title': f"{fwd['weight']} forwards",
        })

    return JsonResponse({
        'nodes': list(nodes.values()),
        'edges': edges,
    })


@login_required
@silk_profile(name='graph.api_user_network')
def api_user_network(request):
    """
    API: User network graph.
    Shows users and their channel memberships.

    Query params:
    - channel_id: Focus on a specific channel
    - user_id: Focus on a specific user
    - hide_bots: Set to '1' to exclude bot accounts
    """
    channel_id = request.GET.get('channel_id')
    user_id = request.GET.get('user_id')
    hide_bots = request.GET.get('hide_bots') == '1'

    user_channels = TelegramChannel.objects.filter(
        active=True
    )
    user_channel_ids = list(user_channels.values_list('id', flat=True))

    nodes = {}
    edges = []

    if user_id:
        # User-centric view
        try:
            target_user = TelegramUser.objects.get(telegram_id=user_id)
        except TelegramUser.DoesNotExist:
            return JsonResponse({'nodes': [], 'edges': [], 'error': 'User not found'})

        nodes[f"u_{target_user.telegram_id}"] = {
            'id': f"u_{target_user.telegram_id}",
            'label': target_user.display_name,
            'type': 'user',
            'telegram_id': target_user.telegram_id,
            'group': 'focus',
            'size': 30,
        }

        memberships = UserGroupMembership.objects.filter(
            user=target_user,
            channel_id__in=user_channel_ids,
            active=True
        ).select_related('channel')

        for membership in memberships:
            channel = membership.channel
            channel_key = f"c_{channel.id}"

            if channel_key not in nodes:
                nodes[channel_key] = {
                    'id': channel_key,
                    'label': channel.title or f"Channel {channel.telegram_id}",
                    'type': 'channel',
                    'telegram_id': channel.telegram_id,
                    'group': 'channel',
                }

            edges.append({
                'from': f"u_{target_user.telegram_id}",
                'to': channel_key,
            })

    elif channel_id:
        # Channel-centric view
        try:
            channel = user_channels.get(id=channel_id)
        except TelegramChannel.DoesNotExist:
            return JsonResponse({'nodes': [], 'edges': [], 'error': 'Channel not found'})

        channel_key = f"c_{channel.id}"
        nodes[channel_key] = {
            'id': channel_key,
            'label': channel.title or f"Channel {channel.telegram_id}",
            'type': 'channel',
            'telegram_id': channel.telegram_id,
            'group': 'focus',
            'size': 30,
        }

        memberships_qs = UserGroupMembership.objects.filter(
            channel=channel,
            active=True
        ).select_related('user')

        if hide_bots:
            memberships_qs = memberships_qs.filter(user__is_bot=False)

        memberships = memberships_qs.order_by('-message_count')[:100]

        for membership in memberships:
            user = membership.user
            user_key = f"u_{user.telegram_id}"

            nodes[user_key] = {
                'id': user_key,
                'label': user.display_name,
                'type': 'user',
                'telegram_id': user.telegram_id,
                'group': 'admin' if membership.is_admin else 'user',
                'message_count': membership.message_count,
            }

            edges.append({
                'from': user_key,
                'to': channel_key,
                'value': membership.message_count or 1,
            })

    else:
        # Overview: channels connected by shared users
        for channel in user_channels[:50]:
            channel_key = f"c_{channel.id}"
            nodes[channel_key] = {
                'id': channel_key,
                'label': channel.title or f"Channel {channel.telegram_id}",
                'type': 'channel',
                'telegram_id': channel.telegram_id,
                'group': 'channel',
            }

        # Find users in multiple channels
        multi_channel_qs = UserGroupMembership.objects.filter(
            channel_id__in=user_channel_ids,
            active=True
        )

        if hide_bots:
            multi_channel_qs = multi_channel_qs.filter(user__is_bot=False)

        multi_channel_users = multi_channel_qs.values('user_id').annotate(
            channel_count=Count('channel_id', distinct=True)
        ).filter(channel_count__gt=1).order_by('-channel_count')[:50]

        multi_user_ids = [u['user_id'] for u in multi_channel_users]

        if multi_user_ids:
            memberships_qs = UserGroupMembership.objects.filter(
                user_id__in=multi_user_ids,
                channel_id__in=user_channel_ids,
                active=True
            ).select_related('user', 'channel')

            if hide_bots:
                memberships_qs = memberships_qs.filter(user__is_bot=False)

            for membership in memberships_qs:
                user = membership.user
                user_key = f"u_{user.telegram_id}"
                channel_key = f"c_{membership.channel_id}"

                if user_key not in nodes:
                    nodes[user_key] = {
                        'id': user_key,
                        'label': user.display_name,
                        'type': 'user',
                        'telegram_id': user.telegram_id,
                        'group': 'bridge',
                    }

                if channel_key in nodes:
                    edges.append({
                        'from': user_key,
                        'to': channel_key,
                    })

    return JsonResponse({
        'nodes': list(nodes.values()),
        'edges': edges,
    })


@login_required
@silk_profile(name='graph.api_crosspost_network')
def api_crosspost_network(request):
    """
    API: Cross-post network.
    Shows users who post in multiple channels.

    Query params:
    - min_channels: Minimum number of channels user must post in (default: 2)
    - min_posts: Minimum total posts across all channels (default: 1)
    """
    min_channels = int(request.GET.get('min_channels', 2))
    min_posts = int(request.GET.get('min_posts', 1))

    user_channel_ids = list(TelegramChannel.objects.filter(
        active=True
    ).values_list('id', flat=True))

    # Find users who post in multiple channels
    crossposters = ArchivedMessage.objects.filter(
        channel_id__in=user_channel_ids,
        sender_id__isnull=False
    ).values('sender_id').annotate(
        channel_count=Count('channel_id', distinct=True),
        message_count=Count('id')
    ).filter(
        channel_count__gte=min_channels,
        message_count__gte=min_posts
    ).order_by('-channel_count', '-message_count')[:100]

    crossposter_ids = [c['sender_id'] for c in crossposters]

    if not crossposter_ids:
        return JsonResponse({'nodes': [], 'edges': []})

    # Get user details
    users = {u.telegram_id: u for u in TelegramUser.objects.filter(telegram_id__in=crossposter_ids)}

    # Get posting details per channel
    posting_data = ArchivedMessage.objects.filter(
        channel_id__in=user_channel_ids,
        sender_id__in=crossposter_ids
    ).values('sender_id', 'channel_id', 'channel__title', 'channel__telegram_id').annotate(
        post_count=Count('id')
    )

    nodes = {}
    edges = []

    # Add channel nodes
    channels_seen = set()
    for post in posting_data:
        channel_key = f"c_{post['channel_id']}"
        if channel_key not in nodes:
            nodes[channel_key] = {
                'id': channel_key,
                'label': post['channel__title'] or f"Channel {post['channel__telegram_id']}",
                'type': 'channel',
                'telegram_id': post['channel__telegram_id'],
                'group': 'channel',
            }

    # Add user nodes and edges
    for cp in crossposters:
        user = users.get(cp['sender_id'])
        user_key = f"u_{cp['sender_id']}"

        nodes[user_key] = {
            'id': user_key,
            'label': user.display_name if user else f"User {cp['sender_id']}",
            'type': 'user',
            'telegram_id': cp['sender_id'],
            'group': 'crossposter',
            'channel_count': cp['channel_count'],
            'message_count': cp['message_count'],
        }

    # Add edges
    for post in posting_data:
        user_key = f"u_{post['sender_id']}"
        channel_key = f"c_{post['channel_id']}"

        edges.append({
            'from': user_key,
            'to': channel_key,
            'value': post['post_count'],
            'title': f"{post['post_count']} posts",
        })

    return JsonResponse({
        'nodes': list(nodes.values()),
        'edges': edges,
    })


@login_required
@silk_profile(name='graph.api_url_network')
def api_url_network(request):
    """
    API: URL sharing network.
    Shows which domains/URLs are shared across channels.

    Query params:
        mode: 'domain' (default) or 'url'
        days: Filter to last N days (0 = all time)
        min_shares: Minimum total shares to include (default 5)
        limit: Max number of domains/URLs to show (default 100)
        search: Filter to specific domain (exact match)
    """
    view_mode = request.GET.get('mode', 'domain')
    days = int(request.GET.get('days', 0))
    min_shares = int(request.GET.get('min_shares', 5))
    limit = int(request.GET.get('limit', 100))
    search_domain = request.GET.get('search', '').strip().lower()

    user_channel_ids = list(TelegramChannel.objects.filter(
        active=True
    ).values_list('id', flat=True))

    # Base filter
    base_filter = Q(message__channel_id__in=user_channel_ids)

    # Time filter
    if days > 0:
        cutoff = timezone.now() - timedelta(days=days)
        base_filter &= Q(message__telegram_date__gte=cutoff)

    # Track domain/URL → channels relationships
    url_channels = defaultdict(lambda: {'channels': defaultdict(int), 'channel_info': {}})

    if view_mode == 'domain':
        domain_filter = base_filter & Q(entity__entity_type='domain')
        # Apply domain search filter
        if search_domain:
            domain_filter &= Q(entity__text__iexact=search_domain)

        domain_entities = MessageEntity.objects.filter(
            domain_filter,
        ).exclude(entity__text='').values(
            'entity__text',
            'message__channel_id',
            'message__channel__title',
            'message__channel__telegram_id',
        ).annotate(count=Count('id'))

        for entity in domain_entities:
            key = entity['entity__text']
            channel_id = entity['message__channel_id']
            url_channels[key]['channels'][channel_id] += entity['count']
            url_channels[key]['channel_info'][channel_id] = {
                'title': entity['message__channel__title'],
                'telegram_id': entity['message__channel__telegram_id'],
            }
    else:
        url_entities = MessageEntity.objects.filter(
            base_filter,
            entity__entity_type__in=['url', 'text_url'],
            entity__url__isnull=False,
        ).exclude(entity__url='').values(
            'entity__url',
            'message__channel_id',
            'message__channel__title',
            'message__channel__telegram_id',
        ).annotate(count=Count('id'))

        for entity in url_entities:
            key = entity['entity__url'][:100]
            channel_id = entity['message__channel_id']
            url_channels[key]['channels'][channel_id] += entity['count']
            url_channels[key]['channel_info'][channel_id] = {
                'title': entity['message__channel__title'],
                'telegram_id': entity['message__channel__telegram_id'],
            }

    # Filter and sort by total shares
    filtered_urls = []
    for url_key, data in url_channels.items():
        total_count = sum(data['channels'].values())
        channel_count = len(data['channels'])
        # When searching, show even single-channel results; otherwise require 2+ channels
        if search_domain:
            # Show all results for searched domain
            filtered_urls.append({
                'key': url_key,
                'total': total_count,
                'channels': data['channels'],
                'channel_info': data['channel_info'],
            })
        elif channel_count > 1 and total_count >= min_shares:
            filtered_urls.append({
                'key': url_key,
                'total': total_count,
                'channels': data['channels'],
                'channel_info': data['channel_info'],
            })

    # Sort by total shares descending and limit
    filtered_urls.sort(key=lambda x: x['total'], reverse=True)
    top_urls = filtered_urls[:limit]

    # Calculate size scaling (min 8, max 40 based on share count)
    max_shares = 1
    min_shares_val = 0
    share_range = 1
    if top_urls:
        max_shares = max(u['total'] for u in top_urls)
        min_shares_val = min(u['total'] for u in top_urls)
        share_range = max_shares - min_shares_val if max_shares > min_shares_val else 1

    nodes = {}
    edges = []

    for url_data in top_urls:
        url_key = url_data['key']
        total_count = url_data['total']

        # Scale node size: 15-50 based on relative share count (larger than channel nodes)
        if share_range > 0:
            size_ratio = (total_count - min_shares_val) / share_range
        else:
            size_ratio = 0.5
        node_size = 15 + (size_ratio * 35)

        url_node_key = f"url_{hash(url_key) % 10000000}"

        nodes[url_node_key] = {
            'id': url_node_key,
            'label': url_key[:40] + ('...' if len(url_key) > 40 else ''),
            'type': 'url',
            'full_url': url_key,
            'group': 'url',
            'channel_count': len(url_data['channels']),
            'total_shares': total_count,
            'size': node_size,
        }

        for channel_id, count in url_data['channels'].items():
            channel_key = f"c_{channel_id}"
            if channel_key not in nodes:
                info = url_data['channel_info'][channel_id]
                nodes[channel_key] = {
                    'id': channel_key,
                    'label': info['title'] or f"Channel {info['telegram_id']}",
                    'type': 'channel',
                    'telegram_id': info['telegram_id'],
                    'group': 'channel',
                    'size': 8,
                }

            edges.append({
                'from': channel_key,
                'to': url_node_key,
                'value': count,
                'title': f"Shared {count} times",
            })

    return JsonResponse({
        'nodes': list(nodes.values()),
        'edges': edges,
        'stats': {
            'total_domains': len(filtered_urls),
            'showing': len(top_urls),
        }
    })


@login_required
@silk_profile(name='graph.api_domain_list')
def api_domain_list(request):
    """
    API: Get list of domains for search dropdown.
    Returns top domains by share count.
    """
    limit = int(request.GET.get('limit', 200))
    search = request.GET.get('q', '').strip().lower()

    user_channel_ids = list(TelegramChannel.objects.filter(
        active=True
    ).values_list('id', flat=True))

    # Get domain counts
    domain_query = MessageEntity.objects.filter(
        message__channel_id__in=user_channel_ids,
        entity__entity_type='domain',
    ).exclude(entity__text='')

    if search:
        domain_query = domain_query.filter(entity__text__icontains=search)

    domains = domain_query.values('entity__text').annotate(
        count=Count('id')
    ).order_by('-count')[:limit]

    return JsonResponse({
        'domains': [{'name': d['entity__text'], 'count': d['count']} for d in domains]
    })


@login_required
@silk_profile(name='graph.api_media_network')
def api_media_network(request):
    """
    API: Shared media network.
    Shows channels that share identical media files with content flow direction.

    Uses file_unique_id (Telegram's stable unique file identifier) to detect
    when the same file is shared across multiple channels.

    Query params:
        days: Filter to last N days (0 = all time, default)
        media_type: Filter by type (all, photo, video, document)
        weight_mode: Edge weight calculation (count, size, recency)
        node_sizing: Node size based on (default, originator, republisher)
    """
    from django.db.models import Min, Sum, Max

    # Parse query params
    days = int(request.GET.get('days', 0))
    media_type = request.GET.get('media_type', 'all')
    weight_mode = request.GET.get('weight_mode', 'count')
    node_sizing = request.GET.get('node_sizing', 'default')

    user_channel_ids = list(TelegramChannel.objects.filter(
        active=True
    ).values_list('id', flat=True))

    # Base filter
    base_filter = Q(
        channel_id__in=user_channel_ids,
        has_media=True,
        file_unique_id__isnull=False
    ) & ~Q(file_unique_id='')

    # Time filter
    if days > 0:
        cutoff = timezone.now() - timedelta(days=days)
        base_filter &= Q(telegram_date__gte=cutoff)

    # Media type filter
    if media_type != 'all':
        base_filter &= Q(media_type=media_type)

    # Find media shared across multiple channels (by file_unique_id)
    shared_media = ArchivedMessage.objects.filter(
        base_filter
    ).values('file_unique_id').annotate(
        channel_count=Count('channel_id', distinct=True),
        total_count=Count('id')
    ).filter(channel_count__gt=1).order_by('-channel_count', '-total_count')[:200]

    if not shared_media:
        return JsonResponse({'nodes': [], 'edges': []})

    shared_file_ids = [m['file_unique_id'] for m in shared_media]

    # Get details for shared media WITH timestamps to determine who posted first
    media_details = ArchivedMessage.objects.filter(
        channel_id__in=user_channel_ids,
        file_unique_id__in=shared_file_ids
    ).values(
        'file_unique_id',
        'channel_id',
        'channel__title',
        'channel__telegram_id',
        'media_type',
        'file_size',
    ).annotate(
        count=Count('id'),
        first_posted=Min('telegram_date'),
        last_posted=Max('telegram_date'),
        total_size=Sum('file_size')
    )

    nodes = {}
    edges = []

    # Track stats for node sizing and coloring
    channel_stats = defaultdict(lambda: {
        'originated': 0,  # Files this channel posted first
        'republished': 0,  # Files this channel reposted from others
        'total_shared': 0,
    })

    # Build media → channel relationships with timing info
    media_channels = defaultdict(list)

    for detail in media_details:
        file_id = detail['file_unique_id']
        channel_id = detail['channel_id']

        # Add channel node
        channel_key = f"c_{channel_id}"
        if channel_key not in nodes:
            nodes[channel_key] = {
                'id': channel_key,
                'label': detail['channel__title'] or f"Channel {detail['channel__telegram_id']}",
                'type': 'channel',
                'telegram_id': detail['channel__telegram_id'],
                'group': 'channel',
            }

        media_channels[file_id].append({
            'channel_id': channel_id,
            'count': detail['count'],
            'media_type': detail['media_type'],
            'first_posted': detail['first_posted'],
            'last_posted': detail['last_posted'],
            'total_size': detail['total_size'] or 0,
        })

    # Track directional relationships: source_channel -> [republisher_channels]
    edge_data = defaultdict(lambda: {
        'count': 0,
        'total_size': 0,
        'latest_date': None
    })

    now = timezone.now()

    # Process each shared file to determine direction and build edges
    for file_id, channels in media_channels.items():
        if len(channels) < 2:
            continue

        # Sort by first_posted to find the original source
        channels_sorted = sorted(channels, key=lambda x: x['first_posted'])
        source_channel = channels_sorted[0]
        republishers = channels_sorted[1:]

        # Track originator/republisher stats
        channel_stats[source_channel['channel_id']]['originated'] += 1
        for rep in republishers:
            channel_stats[rep['channel_id']]['republished'] += 1

        for ch in channels:
            channel_stats[ch['channel_id']]['total_shared'] += 1

        # Create directed edges from source to each republisher
        for rep in republishers:
            edge_key = (source_channel['channel_id'], rep['channel_id'])

            edge_data[edge_key]['count'] += 1
            edge_data[edge_key]['total_size'] += source_channel['total_size']

            # Track most recent repost date for recency weighting
            if edge_data[edge_key]['latest_date'] is None or rep['first_posted'] > edge_data[edge_key]['latest_date']:
                edge_data[edge_key]['latest_date'] = rep['first_posted']

    # Calculate edge values based on weight_mode
    for (from_id, to_id), data in edge_data.items():
        if weight_mode == 'count':
            value = data['count']
            title = f"{data['count']} shared files"
        elif weight_mode == 'size':
            # Convert to MB for display
            size_mb = data['total_size'] / (1024 * 1024)
            value = max(1, int(size_mb))
            title = f"{data['count']} files ({size_mb:.1f} MB)"
        elif weight_mode == 'recency':
            # Weight by recency: more recent = higher value
            if data['latest_date']:
                days_ago = (now - data['latest_date']).days
                # Scale: 0 days = 10x, 30 days = 5x, 90+ days = 1x
                recency_multiplier = max(1, 10 - (days_ago / 10))
                value = int(data['count'] * recency_multiplier)
            else:
                value = data['count']
            title = f"{data['count']} shared files (recent activity weighted)"
        else:
            value = data['count']
            title = f"{data['count']} shared files"

        edges.append({
            'from': f"c_{from_id}",
            'to': f"c_{to_id}",
            'value': value,
            'title': title,
        })

    # Always apply colors based on originated vs republished behavior
    for channel_key, node in nodes.items():
        channel_id = int(channel_key.replace('c_', ''))
        stats = channel_stats[channel_id]

        # Set tooltip with stats
        node['title'] = f"Originated: {stats['originated']}, Republished: {stats['republished']}"

        # Add stats as node properties for the info panel
        node['originated_count'] = stats['originated']
        node['republished_count'] = stats['republished']
        node['total_shared_count'] = stats['total_shared']

        # Color based on dominant behavior
        if stats['originated'] > stats['republished']:
            node['group'] = 'originator'
        elif stats['republished'] > stats['originated']:
            node['group'] = 'republisher'
        # If equal, stays as 'channel' (neutral)

    # Apply node sizing based on mode (only affects size, not color)
    if node_sizing != 'default':
        # Calculate min/max for scaling
        if node_sizing == 'originator':
            values = [s['originated'] for s in channel_stats.values()]
        else:  # republisher
            values = [s['republished'] for s in channel_stats.values()]

        if values:
            min_val = min(values) if values else 0
            max_val = max(values) if values else 1
            val_range = max_val - min_val if max_val > min_val else 1

            for channel_key, node in nodes.items():
                channel_id = int(channel_key.replace('c_', ''))
                stats = channel_stats[channel_id]

                if node_sizing == 'originator':
                    stat_val = stats['originated']
                else:
                    stat_val = stats['republished']

                # Scale size: 10-40 based on relative value
                size_ratio = (stat_val - min_val) / val_range if val_range > 0 else 0.5
                node['size'] = 10 + (size_ratio * 30)

    return JsonResponse({
        'nodes': list(nodes.values()),
        'edges': edges,
        'stats': {
            'total_shared_files': len(shared_file_ids),
            'channels': len(nodes),
        }
    })


@login_required
@silk_profile(name='graph.api_mention_network')
def api_mention_network(request):
    """
    API: Mention network.
    Shows @mention relationships between users.
    """
    user_channel_ids = list(TelegramChannel.objects.filter(
        active=True
    ).values_list('id', flat=True))

    # Get mention entities with user_id (mention_name type)
    mentions = MessageEntity.objects.filter(
        message__channel_id__in=user_channel_ids,
        entity__entity_type='mention_name',
        entity__user_id__isnull=False,
    ).values(
        'message__sender_id',
        'entity__user_id',
        'entity__text',
    ).annotate(count=Count('id')).order_by('-count')[:500]

    if not mentions:
        # Try regular mentions (@username)
        mentions = MessageEntity.objects.filter(
            message__channel_id__in=user_channel_ids,
            entity__entity_type='mention',
        ).exclude(entity__text='').values(
            'message__sender_id',
            'entity__text',
        ).annotate(count=Count('id')).order_by('-count')[:500]

    nodes = {}
    edges = []

    # Collect all user IDs
    user_ids = set()
    for m in mentions:
        if m.get('message__sender_id'):
            user_ids.add(m['message__sender_id'])
        if m.get('entity__user_id'):
            user_ids.add(m['entity__user_id'])

    # Get user details
    users = {u.telegram_id: u for u in TelegramUser.objects.filter(telegram_id__in=user_ids)}

    for mention in mentions:
        sender_id = mention.get('message__sender_id')
        mentioned_id = mention.get('entity__user_id')
        mentioned_username = (mention.get('entity__text') or '').lstrip('@')
        count = mention['count']

        if sender_id:
            sender_key = f"u_{sender_id}"
            if sender_key not in nodes:
                user = users.get(sender_id)
                nodes[sender_key] = {
                    'id': sender_key,
                    'label': user.display_name if user else f"User {sender_id}",
                    'type': 'user',
                    'telegram_id': sender_id,
                    'group': 'mentioner',
                }

            if mentioned_id:
                mentioned_key = f"u_{mentioned_id}"
                if mentioned_key not in nodes:
                    user = users.get(mentioned_id)
                    nodes[mentioned_key] = {
                        'id': mentioned_key,
                        'label': user.display_name if user else f"User {mentioned_id}",
                        'type': 'user',
                        'telegram_id': mentioned_id,
                        'group': 'mentioned',
                    }

                edges.append({
                    'from': sender_key,
                    'to': mentioned_key,
                    'value': count,
                    'title': f"Mentioned {count} times",
                })
            elif mentioned_username:
                # Username mention without user_id
                mentioned_key = f"username_{mentioned_username}"
                if mentioned_key not in nodes:
                    nodes[mentioned_key] = {
                        'id': mentioned_key,
                        'label': f"@{mentioned_username}",
                        'type': 'username',
                        'group': 'mentioned',
                    }

                edges.append({
                    'from': sender_key,
                    'to': mentioned_key,
                    'value': count,
                    'title': f"Mentioned {count} times",
                })

    return JsonResponse({
        'nodes': list(nodes.values()),
        'edges': edges,
    })


@login_required
@silk_profile(name='graph.api_admin_network')
def api_admin_network(request):
    """
    API: Admin network.
    Shows which users are admins of which channels.

    Query params:
    - hide_bots: Set to '1' to exclude bot accounts
    """
    hide_bots = request.GET.get('hide_bots') == '1'

    user_channel_ids = list(TelegramChannel.objects.filter(
        active=True
    ).values_list('id', flat=True))

    # Get admin memberships
    admin_memberships_qs = UserGroupMembership.objects.filter(
        channel_id__in=user_channel_ids,
        is_admin=True,
        active=True
    )

    if hide_bots:
        admin_memberships_qs = admin_memberships_qs.filter(user__is_bot=False)

    admin_memberships = admin_memberships_qs.select_related('user', 'channel')

    nodes = {}
    edges = []

    for membership in admin_memberships:
        user = membership.user
        channel = membership.channel

        user_key = f"u_{user.telegram_id}"
        channel_key = f"c_{channel.id}"

        if user_key not in nodes:
            nodes[user_key] = {
                'id': user_key,
                'label': user.display_name,
                'type': 'user',
                'telegram_id': user.telegram_id,
                'group': 'creator' if membership.is_creator else 'admin',
            }

        if channel_key not in nodes:
            nodes[channel_key] = {
                'id': channel_key,
                'label': channel.title or f"Channel {channel.telegram_id}",
                'type': 'channel',
                'telegram_id': channel.telegram_id,
                'group': 'channel',
            }

        edges.append({
            'from': user_key,
            'to': channel_key,
            'title': membership.admin_title or ('Creator' if membership.is_creator else 'Admin'),
        })

    return JsonResponse({
        'nodes': list(nodes.values()),
        'edges': edges,
    })
