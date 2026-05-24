"""
Notification matcher — invoked from the event processor after
MessageEntity rows are bulk-inserted. Lookups happen against a process-local
cache keyed on (entity_type, lowercased_value), refreshed lazily every 30 s.

For each match we:
  1. Write an ActivityLog row (always-on audit trail).
  2. Create a NotificationDelivery(status='pending') row.
  3. Dispatch deliver_notification.send(delivery.id).
"""

import logging
import time
from typing import Iterable

from django.db.models import F
from django.utils import timezone

from audit.models import ActivityLog, GlobalEntity
from .models import (
    MODE_EVERY,
    MODE_NEW,
    NotificationDelivery,
    STATUS_PENDING,
    WatchlistEntry,
)

logger = logging.getLogger('trawlr.notifications.matcher')

_CACHE_TTL_SECONDS = 30.0
_CACHE_REFRESHED_AT: float = 0.0
_ACTIVE_RULES: dict[tuple[str, str], list[int]] = {}   # (entity_type, value) -> [entry_id, ...]
_ENTRY_BY_ID: dict[int, dict] = {}                     # entry_id -> snapshot

def canonical_value(entity: GlobalEntity) -> str:
    """
    Reduce a GlobalEntity row to the lowercase string the matcher compares
    against WatchlistEntry.entity_value. Mirrors the form's clean_entity_value.

    Note: 'domain' entities (produced by DomainParser) store the domain in the
    `text` field, not `url`, so they share the text-based branch.
    """
    et = entity.entity_type
    if et in ('url', 'text_url'):
        return (entity.url or '').lower()
    if et == 'mention_name':
        return str(entity.user_id or '')
    if et == 'custom_emoji':
        return str(entity.custom_emoji_id or '')
    # domain, mention, hashtag, cashtag, bot_command, email, phone, formatting
    return (entity.text or '').lower()


def _refresh_if_stale(force: bool = False) -> None:
    """Refresh the cached watchlist if older than _CACHE_TTL_SECONDS."""
    global _CACHE_REFRESHED_AT, _ACTIVE_RULES, _ENTRY_BY_ID

    now = time.monotonic()
    if not force and (now - _CACHE_REFRESHED_AT) < _CACHE_TTL_SECONDS:
        return

    rules: dict[tuple[str, str], list[int]] = {}
    by_id: dict[int, dict] = {}

    qs = WatchlistEntry.objects.filter(is_active=True).values(
        'id', 'name', 'mode', 'entity_type', 'entity_value',
        'target_type', 'cooldown_seconds', 'last_triggered_at', 'trigger_count',
    )
    for row in qs:
        # Skip already-fired 'new' rules at cache-build time.
        if row['mode'] == MODE_NEW and row['trigger_count'] > 0:
            continue
        by_id[row['id']] = row
        key = (row['entity_type'], (row['entity_value'] or '').lower())
        rules.setdefault(key, []).append(row['id'])

    _ACTIVE_RULES = rules
    _ENTRY_BY_ID = by_id
    _CACHE_REFRESHED_AT = now


def invalidate_cache() -> None:
    """Force the next match call to reload from DB."""
    global _CACHE_REFRESHED_AT
    _CACHE_REFRESHED_AT = 0.0


def _in_cooldown(snapshot: dict) -> bool:
    cooldown = snapshot.get('cooldown_seconds') or 0
    last = snapshot.get('last_triggered_at')
    if cooldown == 0 or last is None:
        return False
    return (timezone.now() - last).total_seconds() < cooldown


def _build_payload(*, entry_snapshot, mode, entity, archived_msg, message_entity=None) -> dict:
    """Assemble the JSON body that will be POSTed / published."""
    channel = archived_msg.channel
    sender_block = None
    if archived_msg.sender_id:
        sender_block = {
            'telegram_id': archived_msg.sender_id,
            'username': archived_msg.sender_username or '',
            'display_name': archived_msg.sender_name or '',
        }
    return {
        'watchlist_entry': {
            'id': entry_snapshot['id'],
            'name': entry_snapshot['name'],
            'mode': mode,
        },
        'match': {
            'mode': mode,
            'entity': {
                'id': entity.pk if entity is not None else None,
                'type': entity.entity_type if entity is not None else None,
                'text': (entity.text if entity is not None else '') or '',
                'url': (entity.url if entity is not None else '') or '',
                'user_id': entity.user_id if entity is not None else None,
                'first_seen_at': entity.first_seen_at.isoformat() if entity is not None else None,
            },
        },
        'occurrence': {
            'message_id': archived_msg.message_id,
            'channel': {
                'telegram_id': channel.telegram_id if channel else None,
                'username': (channel.username or '') if channel else '',
                'title': (channel.title or '') if channel else '',
            },
            'sender': sender_block,
            'text': archived_msg.text or '',
            'timestamp': archived_msg.telegram_date.isoformat() if archived_msg.telegram_date else None,
            'has_media': bool(archived_msg.has_media),
            'message_entity_id': message_entity.pk if message_entity is not None else None,
            'offset': message_entity.offset if message_entity is not None else None,
            'length': message_entity.length if message_entity is not None else None,
        },
        'trawlr': {
            'delivered_at': timezone.now().isoformat(),
        },
    }

def _log_match(*, entry_snapshot, mode, entity, archived_msg) -> None:
    """Always-on ActivityLog row, independent of external-delivery success."""
    value = canonical_value(entity) if entity is not None else ''
    description = (
        f"Notification '{entry_snapshot['name']}' matched "
        f"{getattr(entity, 'entity_type', '')} {value[:80]}"
    )
    try:
        ActivityLog.log(
            activity_type='entity_match',
            description=description,
            source='notifications',
            channel=archived_msg.channel,
            entry_id=entry_snapshot['id'],
            mode=mode,
            entity_id=entity.pk if entity is not None else None,
            message_id=archived_msg.message_id,
        )
    except Exception as e:  # pragma: no cover - logging must not break ingestion
        logger.debug("Failed to write ActivityLog row for match: %s", e)


def _fire(*, entry_snapshot, mode, entity, archived_msg, message_entity=None) -> None:
    """Atomically claim the trigger (if needed) then create the delivery row."""
    from .tasks import deliver_notification  # local import to avoid actor side effects

    now = timezone.now()

    if mode == MODE_NEW:
        # First-and-only-fire claim. If another worker / earlier batch already
        # bumped trigger_count, the update affects zero rows and we bail out.
        updated = WatchlistEntry.objects.filter(
            pk=entry_snapshot['id'], trigger_count=0,
        ).update(trigger_count=1, last_triggered_at=now)
        if updated == 0:
            entry_snapshot['trigger_count'] = max(entry_snapshot.get('trigger_count', 0), 1)
            return
        entry_snapshot['trigger_count'] = 1
    else:
        WatchlistEntry.objects.filter(pk=entry_snapshot['id']).update(
            trigger_count=F('trigger_count') + 1,
            last_triggered_at=now,
        )
        entry_snapshot['trigger_count'] = entry_snapshot.get('trigger_count', 0) + 1

    entry_snapshot['last_triggered_at'] = now

    _log_match(entry_snapshot=entry_snapshot, mode=mode, entity=entity, archived_msg=archived_msg)

    payload = _build_payload(
        entry_snapshot=entry_snapshot,
        mode=mode,
        entity=entity,
        archived_msg=archived_msg,
        message_entity=message_entity,
    )
    match_context = {
        'mode': mode,
        'entity_id': entity.pk if entity is not None else None,
        'entity_type': entity.entity_type if entity is not None else None,
        'entity_value': canonical_value(entity) if entity is not None else '',
        'message_id': archived_msg.message_id,
        'channel_id': archived_msg.channel_id,
    }
    delivery = NotificationDelivery.objects.create(
        entry_id=entry_snapshot['id'],
        event_payload=payload,
        match_context=match_context,
        status=STATUS_PENDING,
    )

    try:
        deliver_notification.send(delivery.pk)
    except Exception as e:
        # If broker dispatch fails we leave the delivery row in 'pending';
        # the scheduler's retry sweep (Phase 8) can pick it up.
        logger.warning("Failed to dispatch deliver_notification for %s: %s", delivery.pk, e)


def evaluate(*, message_entities: Iterable, archived_msg, newly_created_hashes: set | None = None) -> None:
    """
    Main entry. Called from events.processor._create_message_entities once per
    ingested message. `newly_created_hashes` is accepted for backwards
    compatibility with the processor signature but no longer used — matching
    is purely per-occurrence against (entity_type, value).
    """
    _refresh_if_stale()

    if not _ACTIVE_RULES:
        return

    # Batch-fetch the GlobalEntity rows once so canonical_value() has cheap
    # access to text/url/user_id/etc.
    entity_ids = {me.entity_id for me in message_entities if getattr(me, 'entity_id', None)}
    if not entity_ids:
        return
    entities_by_id = {
        e.pk: e for e in GlobalEntity.objects.filter(pk__in=entity_ids)
    }

    for me in message_entities:
        entity = entities_by_id.get(getattr(me, 'entity_id', None))
        if entity is None:
            continue
        key = (entity.entity_type, canonical_value(entity))
        entry_ids = _ACTIVE_RULES.get(key)
        if not entry_ids:
            continue
        for entry_id in entry_ids:
            snap = _ENTRY_BY_ID.get(entry_id)
            if snap is None:
                continue
            if snap['mode'] == MODE_NEW and snap.get('trigger_count', 0) > 0:
                continue
            if _in_cooldown(snap):
                continue
            _fire(
                entry_snapshot=snap,
                mode=snap['mode'],
                entity=entity,
                archived_msg=archived_msg,
                message_entity=me,
            )


# ---------------------------------------------------------------------------
# Test-delivery helper used by the views.test_entry endpoint and the
# notification_test management command.
# ---------------------------------------------------------------------------

def build_test_delivery(entry: WatchlistEntry) -> NotificationDelivery:
    """
    Create a NotificationDelivery row containing a synthetic payload. The
    actor delivers it through the normal webhook/RMQ path so the user can
    verify their target config end-to-end without waiting for a real match.
    """
    entity_block = {
        'id': None,
        'type': entry.entity_type,
        'text': entry.entity_value,
        'url': entry.entity_value if entry.entity_type in ('url', 'text_url', 'domain') else '',
        'user_id': None,
        'first_seen_at': None,
    }

    payload = {
        'watchlist_entry': {'id': entry.pk, 'name': entry.name, 'mode': entry.mode},
        'match': {'mode': entry.mode, 'entity': entity_block},
        'occurrence': {
            'message_id': None,
            'channel': {'telegram_id': None, 'username': '', 'title': '(test delivery)'},
            'sender': None,
            'text': 'Synthetic test payload from /watchlist/<id>/test/.',
            'timestamp': timezone.now().isoformat(),
            'has_media': False,
        },
        'trawlr': {'delivered_at': None, 'test': True},
    }
    return NotificationDelivery.objects.create(
        entry=entry,
        event_payload=payload,
        match_context={
            'mode': entry.mode,
            'entity_type': entry.entity_type,
            'entity_value': entry.entity_value,
            'test': True,
        },
        status=STATUS_PENDING,
    )
