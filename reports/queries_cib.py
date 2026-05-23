"""
Coordinated Inauthentic Behavior (CIB) detection queries.

Three cluster kinds, identical query shape:
  - text:   GROUP BY ArchivedMessage.content_hash
  - media:  GROUP BY ArchivedMessage.file_unique_id
  - entity: GROUP BY MessageEntity.entity_id (joins through GlobalEntity)

A "cluster" is a fingerprint observed in N+ distinct channels within a
configurable time span. Phase B reuses these queries to render amplifier-chain
timelines (the per-cluster channel list is already returned).

Also exposes get_crossposters() (Phase D) for the user-coordination tab.
"""

from datetime import timedelta

from django.db.models import Count, Max, Min
from django.utils import timezone

from audit.models import MessageEntity
from downloads.models import ArchivedMessage


# Defaults tuned for "obvious coordination" without drowning in marketing repeats.
DEFAULT_DAYS = 7
MAX_DAYS = 30
DEFAULT_MIN_CHANNELS = 3
MIN_MIN_CHANNELS = 2
MAX_MIN_CHANNELS = 50
DEFAULT_WINDOW_SECONDS = 60
MAX_WINDOW_SECONDS = 86400  # 1 day cap; beyond that "coordination" loses meaning.

KIND_TEXT = 'text'
KIND_MEDIA = 'media'
KIND_ENTITY = 'entity'
ALLOWED_KINDS = {KIND_TEXT, KIND_MEDIA, KIND_ENTITY}

TAB_CLUSTERS = 'clusters'
TAB_CHAINS = 'chains'
TAB_CROSSPOSTERS = 'crossposters'
ALLOWED_TABS = {TAB_CLUSTERS, TAB_CHAINS, TAB_CROSSPOSTERS}

# Entity types that meaningfully indicate coordination. Bold/italic/underline
# etc. would generate enormous noise clusters with zero signal.
ENTITY_TYPES_FOR_CLUSTERING = ('url', 'text_url', 'domain', 'mention', 'mention_name', 'hashtag', 'cashtag')

# Hard cap on returned cluster rows so a pathological query can't hose the UI.
ROW_LIMIT = 200


def parse_cib_params(request):
    """Parse + validate CIB query params. Returns a dict consumed by every query."""
    days = _clamp_int(request.GET.get('days'), DEFAULT_DAYS, 1, MAX_DAYS)
    min_channels = _clamp_int(request.GET.get('min_channels'), DEFAULT_MIN_CHANNELS,
                              MIN_MIN_CHANNELS, MAX_MIN_CHANNELS)
    window_seconds = _clamp_int(request.GET.get('window_seconds'), DEFAULT_WINDOW_SECONDS,
                                1, MAX_WINDOW_SECONDS)
    active_only = request.GET.get('active_only', '1').lower() in ('1', 'true', 'yes', 'on')

    kind = request.GET.get('kind', KIND_TEXT)
    if kind not in ALLOWED_KINDS:
        kind = KIND_TEXT

    tab = request.GET.get('tab', TAB_CLUSTERS)
    if tab not in ALLOWED_TABS:
        tab = TAB_CLUSTERS

    now = timezone.now()
    return {
        'days': days,
        'min_channels': min_channels,
        'window_seconds': window_seconds,
        'active_only': active_only,
        'kind': kind,
        'tab': tab,
        'start': now - timedelta(days=days),
        'end': now,
    }


def _clamp_int(raw, default, lo, hi):
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _base_message_qs(params):
    """Time-windowed ArchivedMessage queryset with optional active-account filter."""
    qs = ArchivedMessage.objects.filter(
        telegram_date__gte=params['start'],
        telegram_date__lt=params['end'],
    )
    if params['active_only']:
        qs = qs.from_active_accounts()
    return qs


# =============================================================================
# Cluster queries (Phase A) — also feed Phase B chains
# =============================================================================

def get_clusters(params, kind=None):
    """
    Return a list of cluster rows (max ROW_LIMIT). Each row:
      {
        'kind': 'text'|'media'|'entity',
        'fingerprint': str,
        'n_channels': int,
        'n_messages': int,
        'first_seen': isoformat,
        'last_seen':  isoformat,
        'span_seconds': int,
        'channels': [{'id', 'title', 'first_msg_id', 'first_at'}, ...],
        'sample': {...},  # kind-specific
      }
    """
    kind = kind or params['kind']
    if kind == KIND_MEDIA:
        return _cluster_messages_by(params, 'file_unique_id')
    if kind == KIND_ENTITY:
        return _cluster_entities(params)
    return _cluster_messages_by(params, 'content_hash')


def _cluster_messages_by(params, group_field):
    """Group ArchivedMessage by content_hash or file_unique_id."""
    qs = _base_message_qs(params)
    # Skip rows where the group field is null/empty — they'd cluster as one mega-bucket.
    qs = qs.exclude(**{f'{group_field}__isnull': True}).exclude(**{group_field: ''})

    fingerprints = (
        qs.values(group_field)
        .annotate(
            n_channels=Count('channel_id', distinct=True),
            n_messages=Count('id'),
            first_seen=Min('telegram_date'),
            last_seen=Max('telegram_date'),
        )
        .filter(n_channels__gte=params['min_channels'])
        .order_by('-n_channels', 'first_seen')
    )

    # Span filter is post-aggregation; ORM supports it via F() expressions.
    fingerprints = [
        f for f in fingerprints
        if (f['last_seen'] - f['first_seen']).total_seconds() <= params['window_seconds']
    ][:ROW_LIMIT]

    if not fingerprints:
        return []

    fp_values = [f[group_field] for f in fingerprints]

    # One round-trip to fetch every constituent message we need to render rows.
    # We pull just the columns the cluster row needs.
    detail_rows = (
        _base_message_qs(params)
        .filter(**{f'{group_field}__in': fp_values})
        .values(
            group_field, 'channel_id', 'channel__title', 'channel__username',
            'message_id', 'telegram_date', 'text', 'file_unique_id', 'media_type',
        )
        .order_by('telegram_date', 'message_id')
    )

    by_fp = {fp: [] for fp in fp_values}
    for r in detail_rows:
        by_fp[r[group_field]].append(r)

    clusters = []
    for f in fingerprints:
        fp = f[group_field]
        rows = by_fp.get(fp, [])
        if not rows:
            continue
        sample = _kind_sample(group_field, rows[0])
        clusters.append({
            'kind': KIND_TEXT if group_field == 'content_hash' else KIND_MEDIA,
            'fingerprint': fp,
            'n_channels': f['n_channels'],
            'n_messages': f['n_messages'],
            'first_seen': f['first_seen'].isoformat(),
            'last_seen': f['last_seen'].isoformat(),
            'span_seconds': int((f['last_seen'] - f['first_seen']).total_seconds()),
            'channels': order_chain(rows),
            'sample': sample,
        })

    return clusters


def _cluster_entities(params):
    """Group MessageEntity by entity_id, restricted to high-signal entity types."""
    base = MessageEntity.objects.filter(
        message__telegram_date__gte=params['start'],
        message__telegram_date__lt=params['end'],
        entity__entity_type__in=ENTITY_TYPES_FOR_CLUSTERING,
    )
    if params['active_only']:
        base = base.filter(message__channel__account__is_active=True)

    fingerprints = (
        base.values('entity_id')
        .annotate(
            n_channels=Count('channel_id', distinct=True),
            n_messages=Count('id'),
            first_seen=Min('message__telegram_date'),
            last_seen=Max('message__telegram_date'),
        )
        .filter(n_channels__gte=params['min_channels'])
        .order_by('-n_channels', 'first_seen')
    )

    fingerprints = [
        f for f in fingerprints
        if (f['last_seen'] - f['first_seen']).total_seconds() <= params['window_seconds']
    ][:ROW_LIMIT]

    if not fingerprints:
        return []

    entity_ids = [f['entity_id'] for f in fingerprints]
    detail_rows = (
        base.filter(entity_id__in=entity_ids)
        .values(
            'entity_id',
            'entity__content_hash', 'entity__entity_type', 'entity__text', 'entity__url',
            'channel_id', 'message__channel__title', 'message__channel__username',
            'message_id', 'message__telegram_date',
        )
        .order_by('message__telegram_date', 'message_id')
    )

    by_eid = {eid: [] for eid in entity_ids}
    entity_meta = {}
    for r in detail_rows:
        eid = r['entity_id']
        # Re-shape so order_chain sees the same field names.
        by_eid[eid].append({
            'channel_id': r['channel_id'],
            'channel__title': r['message__channel__title'],
            'channel__username': r['message__channel__username'],
            'message_id': r['message_id'],
            'telegram_date': r['message__telegram_date'],
        })
        if eid not in entity_meta:
            entity_meta[eid] = {
                'content_hash': r['entity__content_hash'],
                'entity_type': r['entity__entity_type'],
                'text': r['entity__text'],
                'url': r['entity__url'],
            }

    clusters = []
    for f in fingerprints:
        eid = f['entity_id']
        meta = entity_meta.get(eid)
        if not meta:
            continue
        clusters.append({
            'kind': KIND_ENTITY,
            'fingerprint': meta['content_hash'],
            'n_channels': f['n_channels'],
            'n_messages': f['n_messages'],
            'first_seen': f['first_seen'].isoformat(),
            'last_seen': f['last_seen'].isoformat(),
            'span_seconds': int((f['last_seen'] - f['first_seen']).total_seconds()),
            'channels': order_chain(by_eid[eid]),
            'sample': {
                'entity_type': meta['entity_type'],
                'entity_text': meta['text'] or meta['url'] or '',
            },
        })

    return clusters


def _kind_sample(group_field, row):
    """First-message snapshot used as the human-readable sample on a cluster row."""
    if group_field == 'file_unique_id':
        return {
            'file_unique_id': row['file_unique_id'],
            'media_type': row['media_type'],
            'text': (row['text'] or '')[:200],
        }
    return {'text': (row['text'] or '')[:200]}


def order_chain(rows):
    """
    Collapse per-message rows into per-channel timeline entries, sorted by
    first appearance. Output:

        [{'id', 'title', 'username', 'first_msg_id', 'first_at',
          'delta_seconds', 'n_messages_in_cluster'}, ...]

    delta_seconds is relative to the originator (first channel to post).
    Phase B's swim-lane uses this directly.
    """
    if not rows:
        return []

    # Earliest row per channel.
    by_channel = {}
    counts = {}
    for r in rows:
        cid = r['channel_id']
        counts[cid] = counts.get(cid, 0) + 1
        existing = by_channel.get(cid)
        if existing is None or r['telegram_date'] < existing['telegram_date']:
            by_channel[cid] = r

    ordered = sorted(by_channel.values(), key=lambda x: x['telegram_date'])
    originator_at = ordered[0]['telegram_date']

    return [
        {
            'id': r['channel_id'],
            'title': r.get('channel__title') or r.get('channel__username') or f'#{r["channel_id"]}',
            'username': r.get('channel__username') or '',
            'first_msg_id': r['message_id'],
            'first_at': r['telegram_date'].isoformat(),
            'delta_seconds': int((r['telegram_date'] - originator_at).total_seconds()),
            'n_messages_in_cluster': counts[r['channel_id']],
        }
        for r in ordered
    ]


# =============================================================================
# Speed crossposters (Phase D)
# =============================================================================

def get_crossposters(params):
    """
    Users who post the same content (text or media) across N+ channels in a
    tight time span. Distinct from amplifier chains (which group by content
    across many users); this groups by (sender, content) — a single human
    operating multiple channels.
    """
    qs = _base_message_qs(params).filter(sender_id__isnull=False)

    # Two passes: text and media. We union and rank.
    text_pass = _crossposter_pass(qs, 'content_hash', params)
    media_pass = _crossposter_pass(qs, 'file_unique_id', params)

    rows = text_pass + media_pass
    rows.sort(key=lambda r: (-r['n_channels'], r['span_seconds']))
    return rows[:ROW_LIMIT]


def _crossposter_pass(qs, group_field, params):
    qs = qs.exclude(**{f'{group_field}__isnull': True}).exclude(**{group_field: ''})

    pairs = (
        qs.values('sender_id', group_field)
        .annotate(
            n_channels=Count('channel_id', distinct=True),
            n_messages=Count('id'),
            first_seen=Min('telegram_date'),
            last_seen=Max('telegram_date'),
        )
        .filter(n_channels__gte=params['min_channels'])
    )

    pairs = [
        p for p in pairs
        if (p['last_seen'] - p['first_seen']).total_seconds() <= params['window_seconds']
    ]

    if not pairs:
        return []

    # Detail fetch for each (sender, fingerprint) pair, capped to avoid huge fan-out.
    rows_out = []
    for p in pairs[:ROW_LIMIT]:
        msgs = (
            qs.filter(sender_id=p['sender_id'], **{group_field: p[group_field]})
            .values(
                'channel_id', 'channel__title', 'channel__username',
                'message_id', 'telegram_date', 'text',
                'sender_id', 'sender_name', 'sender_username',
                'file_unique_id', 'media_type',
            )
            .order_by('telegram_date', 'message_id')
        )
        msg_list = list(msgs)
        if not msg_list:
            continue
        sample = _kind_sample(group_field, msg_list[0])
        first = msg_list[0]
        rows_out.append({
            'kind': 'crossposter',
            'fingerprint': f"{p['sender_id']}:{p[group_field]}",
            'sender': {
                'id': p['sender_id'],
                'name': first.get('sender_name') or '',
                'username': first.get('sender_username') or '',
            },
            'content_kind': KIND_MEDIA if group_field == 'file_unique_id' else KIND_TEXT,
            'n_channels': p['n_channels'],
            'n_messages': p['n_messages'],
            'first_seen': p['first_seen'].isoformat(),
            'last_seen': p['last_seen'].isoformat(),
            'span_seconds': int((p['last_seen'] - p['first_seen']).total_seconds()),
            'channels': order_chain(msg_list),
            'sample': sample,
        })

    return rows_out
