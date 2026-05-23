"""
Per-user network views — queries powering the Network tab on a user profile.

Three panels, three query functions, all scoped to a single TelegramUser:

  - get_group_lanes()    swim-lane timeline: per-channel pin row across a window
  - get_content_flow()   sankey: inbound forward sources -> user -> outbound destinations
  - get_co_posters()     small graph of other users posting same content within window

Visually mirrors the CIB amplifier-chain page (templates/cib/page.html) but
with the user as the fixed pivot rather than a fingerprint cluster. The
existing user Activity heatmap answers "WHEN does this user post"; this
module answers "WHAT GROUPS / WHO ELSE / WHAT FLOWS".
"""

from collections import defaultdict
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from audit.models import (
    ForwardSource,
    TelegramChannel,
    TelegramUser,
    UserGroupMembership,
)
from downloads.models import ArchivedMessage


DEFAULT_DAYS = 30
ALLOWED_DAYS = {7, 30, 90}

# Swim-lane caps so a chatty bot can't render 10k DOM nodes.
LANES_MAX_GROUPS = 12
LANES_MAX_PINS_PER_GROUP = 200

# Sankey caps — keep the graph readable.
FLOW_MAX_NODES_PER_SIDE = 10

# Co-poster window defaults.
COPOST_DEFAULT_WINDOW_SECONDS = 300  # +/- 5 minutes
COPOST_MAX_WINDOW_SECONDS = 3600
COPOST_DEFAULT_MIN_OVERLAP = 3
COPOST_MAX_PEERS = 25


# ---------------------------------------------------------------------------
# Param parsing
# ---------------------------------------------------------------------------

def parse_days(request, default=DEFAULT_DAYS):
    try:
        d = int(request.GET.get('days', default))
    except (TypeError, ValueError):
        return default
    return d if d in ALLOWED_DAYS else default


def parse_window_seconds(request, default=COPOST_DEFAULT_WINDOW_SECONDS):
    try:
        v = int(request.GET.get('window_seconds', default))
    except (TypeError, ValueError):
        return default
    return max(30, min(COPOST_MAX_WINDOW_SECONDS, v))


def parse_min_overlap(request, default=COPOST_DEFAULT_MIN_OVERLAP):
    try:
        v = int(request.GET.get('min_overlap', default))
    except (TypeError, ValueError):
        return default
    return max(2, min(50, v))


def window_bounds(days):
    end = timezone.now()
    start = end - timedelta(days=days)
    return start, end


# ---------------------------------------------------------------------------
# Panel 1: Group activity swim lanes
# ---------------------------------------------------------------------------

def get_group_lanes(telegram_user: TelegramUser, days: int):
    """
    For each of the user's top groups in the window, return a row of timestamped
    pins. Pin kinds:
      - 'post'    plain post
      - 'forward' message that has a ForwardSource attached
      - 'edit'    edited message (telegram_date kept; edit_at also returned)
      - 'delete'  message later deleted (uses deleted_at)

    Returned shape:
      {
        'days': int,
        'start': iso, 'end': iso,
        'groups': [
          {'id', 'title', 'username', 'count',
           'pins': [{'ts': iso, 'kind': str, 'message_id': int}, ...]},
          ...
        ],
      }
    """
    start, end = window_bounds(days)

    base = (
        ArchivedMessage.objects.from_active_accounts()
        .filter(
            sender_id=telegram_user.telegram_id,
            telegram_date__gte=start,
            telegram_date__lt=end,
        )
    )

    top_channels = list(
        base.values('channel_id', 'channel__title', 'channel__username')
        .annotate(count=Count('id'))
        .order_by('-count')[:LANES_MAX_GROUPS]
    )
    if not top_channels:
        return {
            'days': days,
            'start': start.isoformat(),
            'end': end.isoformat(),
            'groups': [],
        }

    channel_ids = [c['channel_id'] for c in top_channels]

    # Pull pins for those channels in one query, capped per channel by a Python
    # truncation (Postgres window-function ranking would be cleaner but this is
    # well within budget for LANES_MAX_GROUPS x LANES_MAX_PINS_PER_GROUP rows).
    msg_rows = list(
        base.filter(channel_id__in=channel_ids)
        .values(
            'channel_id', 'message_id', 'telegram_date',
            'edited_date', 'deleted_at',
        )
        .annotate(has_fwd=Count('forward_source'))
        .order_by('telegram_date')
    )

    pins_by_channel = defaultdict(list)
    for r in msg_rows:
        ch_id = r['channel_id']
        if len(pins_by_channel[ch_id]) >= LANES_MAX_PINS_PER_GROUP:
            continue

        kind = 'post'
        if r['has_fwd']:
            kind = 'forward'
        elif r['deleted_at']:
            kind = 'delete'
        elif r['edited_date']:
            kind = 'edit'

        pins_by_channel[ch_id].append({
            'ts': r['telegram_date'].isoformat(),
            'kind': kind,
            'message_id': r['message_id'],
        })

    groups = []
    for c in top_channels:
        ch_id = c['channel_id']
        groups.append({
            'id': ch_id,
            'title': c['channel__title'] or c['channel__username'] or f'#{ch_id}',
            'username': c['channel__username'] or '',
            'count': c['count'],
            'pins': pins_by_channel.get(ch_id, []),
        })

    return {
        'days': days,
        'start': start.isoformat(),
        'end': end.isoformat(),
        'groups': groups,
    }


# ---------------------------------------------------------------------------
# Panel 2: Content flow sankey
# ---------------------------------------------------------------------------

def get_content_flow(telegram_user: TelegramUser, days: int):
    """
    Return a sankey-shaped view: inbound forward sources -> user -> outbound
    channels that re-broadcast the user's content.

    Inbound  = ForwardSource attached to messages the user posted.
               Each row represents 'user re-broadcast something originating from X'.
               Grouped by source_telegram_id (falls back to source_username/from_name).
    Outbound = ForwardSource rows whose source_telegram_id == this user
               (so 'this user's content was forwarded by someone' into channel Y).
               Grouped by destination channel_id.

    Self-counts:
      - inbound 'own' bucket: messages the user posted with no forward_source
                              (their original content) — included so the centre
                              column total matches their post count.
    """
    start, end = window_bounds(days)

    user_messages = ArchivedMessage.objects.from_active_accounts().filter(
        sender_id=telegram_user.telegram_id,
        telegram_date__gte=start,
        telegram_date__lt=end,
    )

    user_total = user_messages.count()

    # --- Inbound ---------------------------------------------------------
    fwd_in = (
        ForwardSource.objects.filter(message__in=user_messages)
        .values('source_telegram_id', 'source_title', 'source_username',
                'from_name', 'source_type')
        .annotate(count=Count('id'))
        .order_by('-count')
    )

    inbound_buckets = defaultdict(lambda: {
        'count': 0, 'label': '', 'source_id': None, 'source_type': '',
    })
    for r in fwd_in:
        sid = r['source_telegram_id']
        if sid:
            key = ('id', sid)
            label = r['source_title'] or r['source_username'] or f'id {sid}'
        else:
            label = r['source_title'] or r['from_name'] or '(hidden)'
            key = ('name', label)
        b = inbound_buckets[key]
        b['count'] += r['count']
        b['label'] = label
        b['source_id'] = sid
        b['source_type'] = r['source_type']

    inbound = sorted(
        ({'label': v['label'], 'count': v['count'],
          'source_id': v['source_id'], 'source_type': v['source_type']}
         for v in inbound_buckets.values()),
        key=lambda x: x['count'],
        reverse=True,
    )
    inbound_top = inbound[:FLOW_MAX_NODES_PER_SIDE]
    inbound_other = sum(x['count'] for x in inbound[FLOW_MAX_NODES_PER_SIDE:])
    if inbound_other:
        inbound_top.append({
            'label': f'+{len(inbound) - FLOW_MAX_NODES_PER_SIDE} more',
            'count': inbound_other,
            'source_id': None, 'source_type': 'other',
        })

    fwd_in_total = sum(x['count'] for x in inbound)
    own_count = max(0, user_total - fwd_in_total)
    if own_count:
        inbound_top.insert(0, {
            'label': '(original content)',
            'count': own_count,
            'source_id': None,
            'source_type': 'self',
        })

    # --- Outbound --------------------------------------------------------
    fwd_out = (
        ForwardSource.objects.filter(
            source_telegram_id=telegram_user.telegram_id,
            message__telegram_date__gte=start,
            message__telegram_date__lt=end,
            message__channel__account__is_active=True,
        )
        .values('message__channel_id',
                'message__channel__title',
                'message__channel__username')
        .annotate(count=Count('id'))
        .order_by('-count')
    )
    outbound = [
        {
            'channel_id': r['message__channel_id'],
            'label': (r['message__channel__title']
                      or r['message__channel__username']
                      or f'#{r["message__channel_id"]}'),
            'count': r['count'],
        }
        for r in fwd_out
    ]
    outbound_top = outbound[:FLOW_MAX_NODES_PER_SIDE]
    outbound_other = sum(x['count'] for x in outbound[FLOW_MAX_NODES_PER_SIDE:])
    if outbound_other:
        outbound_top.append({
            'channel_id': None,
            'label': f'+{len(outbound) - FLOW_MAX_NODES_PER_SIDE} more',
            'count': outbound_other,
        })

    return {
        'days': days,
        'user_total': user_total,
        'inbound': inbound_top,
        'outbound': outbound_top,
        'totals': {
            'inbound_forwards': fwd_in_total,
            'outbound_forwards': sum(x['count'] for x in outbound),
            'own_posts': own_count,
        },
    }


# ---------------------------------------------------------------------------
# Panel 3: Co-poster mini-graph
# ---------------------------------------------------------------------------

def get_co_posters(
    telegram_user: TelegramUser,
    days: int,
    window_seconds: int,
    min_overlap: int,
):
    """
    Find other users who posted the same content (same content_hash or same
    file_unique_id) within +/- window_seconds of this user, then return a
    vis.js-shaped {nodes, edges} graph centred on this user.

    Heavy-ish query, so all the caps live in module constants.
    """
    start, end = window_bounds(days)

    user_msgs = list(
        ArchivedMessage.objects.from_active_accounts()
        .filter(
            sender_id=telegram_user.telegram_id,
            telegram_date__gte=start,
            telegram_date__lt=end,
        )
        .filter(Q(content_hash__isnull=False) | Q(file_unique_id__gt=''))
        .values('telegram_date', 'content_hash', 'file_unique_id', 'channel_id')
    )
    if not user_msgs:
        return {'nodes': [], 'edges': [], 'window_seconds': window_seconds,
                'min_overlap': min_overlap, 'days': days}

    # Build per-fingerprint candidate lookups in batches to keep IN-clauses sane.
    text_hashes = list({m['content_hash'] for m in user_msgs if m['content_hash']})
    media_hashes = list({m['file_unique_id'] for m in user_msgs if m['file_unique_id']})

    candidate_qs = ArchivedMessage.objects.from_active_accounts().filter(
        telegram_date__gte=start - timedelta(seconds=window_seconds),
        telegram_date__lt=end + timedelta(seconds=window_seconds),
    ).exclude(sender_id=telegram_user.telegram_id).exclude(sender_id__isnull=True)

    fp_filter = Q()
    if text_hashes:
        fp_filter |= Q(content_hash__in=text_hashes)
    if media_hashes:
        fp_filter |= Q(file_unique_id__in=media_hashes)
    candidates = list(
        candidate_qs.filter(fp_filter).values(
            'sender_id', 'sender_username', 'sender_name',
            'telegram_date', 'content_hash', 'file_unique_id', 'channel_id',
        )
    )

    # Index candidates by fingerprint for O(1) match lookup per user message.
    by_text = defaultdict(list)
    by_media = defaultdict(list)
    for c in candidates:
        if c['content_hash']:
            by_text[c['content_hash']].append(c)
        if c['file_unique_id']:
            by_media[c['file_unique_id']].append(c)

    edge_counts = defaultdict(int)              # peer_sender_id -> co-post count
    peer_meta = {}                              # peer_sender_id -> {label, channels}
    peer_channels = defaultdict(set)

    for m in user_msgs:
        ts = m['telegram_date']
        cands = []
        if m['content_hash']:
            cands.extend(by_text.get(m['content_hash'], ()))
        if m['file_unique_id']:
            cands.extend(by_media.get(m['file_unique_id'], ()))
        for c in cands:
            delta = abs((c['telegram_date'] - ts).total_seconds())
            if delta > window_seconds:
                continue
            sid = c['sender_id']
            edge_counts[sid] += 1
            if sid not in peer_meta:
                peer_meta[sid] = {
                    'label': (f"@{c['sender_username']}" if c['sender_username']
                              else (c['sender_name'] or f'id {sid}')),
                }
            peer_channels[sid].add(c['channel_id'])

    # Threshold + cap
    ranked = sorted(
        ((sid, n) for sid, n in edge_counts.items() if n >= min_overlap),
        key=lambda x: x[1],
        reverse=True,
    )[:COPOST_MAX_PEERS]
    if not ranked:
        return {'nodes': [], 'edges': [], 'window_seconds': window_seconds,
                'min_overlap': min_overlap, 'days': days}

    # Map peer sender_id -> TelegramUser pk so the front-end can deep-link.
    peer_ids = [sid for sid, _ in ranked]
    peer_pks = dict(
        TelegramUser.objects.filter(telegram_id__in=peer_ids)
        .values_list('telegram_id', 'pk')
    )

    centre_label = (f"@{telegram_user.username}" if telegram_user.username
                    else (telegram_user.display_name or f'id {telegram_user.telegram_id}'))

    nodes = [{
        'id': f'u{telegram_user.telegram_id}',
        'label': centre_label,
        'type': 'user',
        'group': 'focus',
        'size': 26,
        'user_pk': telegram_user.pk,
        'telegram_id': telegram_user.telegram_id,
        'message_count': len(user_msgs),
    }]
    edges = []
    for sid, n in ranked:
        meta = peer_meta[sid]
        nodes.append({
            'id': f'u{sid}',
            'label': meta['label'],
            'type': 'user',
            'group': 'crossposter',
            'size': 12 + min(20, n),
            'user_pk': peer_pks.get(sid),
            'telegram_id': sid,
            'channel_count': len(peer_channels[sid]),
            'message_count': n,
        })
        edges.append({
            'from': f'u{telegram_user.telegram_id}',
            'to': f'u{sid}',
            'value': n,
            'title': f'{n} co-posts within ±{window_seconds}s',
        })

    return {
        'nodes': nodes,
        'edges': edges,
        'window_seconds': window_seconds,
        'min_overlap': min_overlap,
        'days': days,
    }
