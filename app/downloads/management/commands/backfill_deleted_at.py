"""
Backfill ArchivedMessage.deleted_at from RawEvents (event_type='message_deleted').

RawEvents.event_timestamp preserves the listener's receive time — i.e. the
moment Telegram delivered the deletion update — so it is the only source we
trust as a real "deletion time". We deliberately do NOT use the simple-history
table here, because history_date is save-time, not deletion-time, and would
produce misleading results.

Earliest message_deleted RawEvent per (chat_id, message_id) wins. Existing
non-null deleted_at values are never overwritten.
"""

import time
from collections import defaultdict
from datetime import timezone as dt_timezone

from django.core.management.base import BaseCommand
from django.db.models import Q

from downloads.models import ArchivedMessage
from events.models import RawEvents


PROGRESS_EVERY = 1000
BULK_UPDATE_BATCH = 500


def _build_chat_id_lookup(chat_id):
    """Mirror events.processor._build_chat_id_lookup so backfill matches live processing."""
    abs_id = abs(chat_id)
    lookup_ids = [chat_id, abs_id, -abs_id, int(f"-100{abs_id}")]
    chat_str = str(chat_id)
    if chat_str.startswith('-100') and len(chat_str) > 4:
        lookup_ids.append(int(chat_str[4:]))
        lookup_ids.append(-int(chat_str[4:]))
    return list(dict.fromkeys(lookup_ids))


class Command(BaseCommand):
    help = 'Backfill ArchivedMessage.deleted_at from RawEvents.event_timestamp'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would be updated without writing')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        t0 = time.monotonic()

        # ---------- Phase 1: collect earliest event_timestamp per (chat_id, message_id) ----------
        events_qs = (
            RawEvents.objects
            .filter(event_type='message_deleted')
            .only('event_timestamp', 'raw_json')
            .order_by('event_timestamp')
        )
        total_events = events_qs.count()
        self.stdout.write(f'Phase 1/2: scanning {total_events:,} message_deleted raw events...')

        earliest_by_msg = {}  # (chat_id, message_id) -> event_timestamp
        scanned = 0
        for ev in events_qs.iterator(chunk_size=2000):
            scanned += 1
            payload = ev.raw_json or {}
            chat_id = payload.get('chat_id')
            message_ids = payload.get('message_ids') or []
            if chat_id is not None and message_ids:
                ts = ev.event_timestamp
                for mid in message_ids:
                    key = (chat_id, mid)
                    existing = earliest_by_msg.get(key)
                    if existing is None or ts < existing:
                        earliest_by_msg[key] = ts
            if scanned % PROGRESS_EVERY == 0:
                rate = scanned / max(time.monotonic() - t0, 1e-6)
                self.stdout.write(
                    f'  scanned {scanned:,}/{total_events:,} '
                    f'({len(earliest_by_msg):,} unique msgs, {rate:,.0f}/s)'
                )

        if not earliest_by_msg:
            self.stdout.write(self.style.WARNING('No message_deleted events to backfill from.'))
            return

        self.stdout.write(
            f'Phase 1 done: {len(earliest_by_msg):,} unique (chat,msg) pairs '
            f'in {time.monotonic() - t0:.1f}s'
        )

        # ---------- Phase 2: bulk-update ArchivedMessage rows in chunks ----------
        by_chat = defaultdict(dict)  # chat_id -> {message_id: deleted_at}
        for (chat_id, mid), ts in earliest_by_msg.items():
            by_chat[chat_id][mid] = ts

        total_targets = sum(len(m) for m in by_chat.values())
        self.stdout.write(
            f'Phase 2/2: applying updates across {len(by_chat):,} chats / '
            f'{total_targets:,} candidate (chat,msg) pairs...'
        )

        updated = 0
        skipped_already_set = 0
        no_match = 0
        processed = 0
        t_phase2 = time.monotonic()

        for chat_id, msg_map in by_chat.items():
            lookup_ids = _build_chat_id_lookup(chat_id)
            chan_q = Q()
            for lid in lookup_ids:
                chan_q |= Q(channel__telegram_id=lid)

            # Single fetch: only rows we actually need (deleted_at IS NULL). On a
            # second run, this returns nothing for already-backfilled chats.
            rows = list(
                ArchivedMessage.objects
                .filter(chan_q, message_id__in=msg_map.keys())
                .values_list('id', 'message_id', 'deleted_at')
            )
            matched_msg_ids = {mid for _, mid, _ in rows}
            no_match += len(msg_map.keys() - matched_msg_ids)

            msgs = []
            for pk, mid, existing_ts in rows:
                if existing_ts is not None:
                    skipped_already_set += 1
                    continue
                msgs.append((pk, mid))

            to_update = []
            for pk, mid in msgs:
                processed += 1
                ts = msg_map.get(mid)
                if ts is None:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt_timezone.utc)
                # Construct a sparse instance for bulk_update — only the listed
                # fields get written, so we don't need to hydrate the full row.
                to_update.append(ArchivedMessage(pk=pk, is_deleted=True, deleted_at=ts))

                if processed % PROGRESS_EVERY == 0:
                    rate = processed / max(time.monotonic() - t_phase2, 1e-6)
                    self.stdout.write(
                        f'  processed {processed:,} (queued {len(to_update):,} for write, '
                        f'{updated:,} done, {rate:,.0f}/s)'
                    )

            if to_update and not dry_run:
                ArchivedMessage.objects.bulk_update(
                    to_update,
                    fields=['is_deleted', 'deleted_at'],
                    batch_size=BULK_UPDATE_BATCH,
                )
            updated += len(to_update)

        elapsed = time.monotonic() - t0
        prefix = 'DRY RUN — would set' if dry_run else 'Set'
        self.stdout.write(self.style.SUCCESS(
            f'{prefix} deleted_at on {updated:,} messages in {elapsed:.1f}s '
            f'(skipped {skipped_already_set:,} already set, '
            f'{no_match:,} raw-event entries had no matching ArchivedMessage)'
        ))
