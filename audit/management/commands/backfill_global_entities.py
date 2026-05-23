"""
Backfill MessageEntity.entity FK for rows that existed before Phase 2 dual-write.

Reads the legacy (entity_type, text, url, user_id, custom_emoji_id, language)
columns directly via SQL — the Phase 5 model no longer declares them but the
columns are still on the table until migration 0050 runs. This lets the
command work regardless of migration state, as long as at least 0049 has
been applied.

Safe to stop and restart — the filter ``WHERE entity_id IS NULL`` is the
only progress marker, so each restart picks up exactly where it stopped.

Usage:
    python manage.py backfill_global_entities [--batch-size N] [--limit N]
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from audit.models import GlobalEntity


# Order must match the SELECT below so we can unpack directly into compute_hash()
_LEGACY_COLUMNS = (
    'id', 'entity_type', 'text', 'url', 'user_id', 'custom_emoji_id', 'language',
)


def _legacy_columns_exist() -> bool:
    """True if the pre-0050 duplicated columns still exist on audit_messageentity."""
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'audit_messageentity'
              AND column_name = ANY(%s)
            """,
            [list(_LEGACY_COLUMNS[1:])],  # skip 'id'
        )
        return cur.rowcount == len(_LEGACY_COLUMNS) - 1


def _fetch_null_batch(batch_size: int):
    """Return list of tuples (id, entity_type, text, url, user_id, custom_emoji_id, language)."""
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join(_LEGACY_COLUMNS)}
            FROM audit_messageentity
            WHERE entity_id IS NULL
            ORDER BY id
            LIMIT %s
            """,
            [batch_size],
        )
        return cur.fetchall()


def _count_nulls() -> int:
    with connection.cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM audit_messageentity WHERE entity_id IS NULL')
        return cur.fetchone()[0]


def _update_entity_ids(pk_to_entity_id: dict[int, int]):
    """One UPDATE per (entity_id, [pks]) group."""
    by_entity: dict[int, list[int]] = {}
    for pk, eid in pk_to_entity_id.items():
        by_entity.setdefault(eid, []).append(pk)
    with connection.cursor() as cur:
        for entity_id, pks in by_entity.items():
            cur.execute(
                'UPDATE audit_messageentity SET entity_id = %s WHERE id = ANY(%s)',
                [entity_id, pks],
            )


class Command(BaseCommand):
    help = 'Backfill MessageEntity.entity FK by deduplicating into GlobalEntity.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--batch-size', type=int, default=10_000,
            help='Number of MessageEntity rows to process per batch (default: 10000)'
        )
        parser.add_argument(
            '--limit', type=int, default=0,
            help='Stop after this many rows have been backfilled (0 = all, default)'
        )

    def handle(self, *args, **options):
        if not _legacy_columns_exist():
            raise CommandError(
                'Legacy columns (entity_type, text, url, ...) are missing from '
                'audit_messageentity. Migration 0050 has already run and dropped '
                'them — backfill is not possible from this state. If rows still '
                'have entity_id IS NULL, you need a restore or rebuild strategy.'
            )

        batch_size = options['batch_size']
        limit = options['limit']

        total_remaining = _count_nulls()
        self.stdout.write(self.style.NOTICE(
            f'MessageEntity rows needing backfill: {total_remaining:,}'
        ))
        if total_remaining == 0:
            self.stdout.write(self.style.SUCCESS('Nothing to do.'))
            return

        processed = 0
        created_entities = 0
        while True:
            rows = _fetch_null_batch(batch_size)
            if not rows:
                break

            entity_dicts = []
            pk_to_hash: dict[int, str] = {}
            for pk, entity_type, text, url, user_id, custom_emoji_id, language in rows:
                d = {
                    'entity_type': entity_type,
                    'text': text or '',
                    'url': url or '',
                    'user_id': user_id,
                    'custom_emoji_id': custom_emoji_id,
                    'language': language or '',
                }
                entity_dicts.append(d)
                pk_to_hash[pk] = GlobalEntity.compute_hash(**d)

            before_count = GlobalEntity.objects.count()
            hash_to_id = GlobalEntity.bulk_get_or_create(entity_dicts)
            after_count = GlobalEntity.objects.count()
            created_entities += (after_count - before_count)

            pk_to_entity_id = {pk: hash_to_id[h] for pk, h in pk_to_hash.items()}

            with transaction.atomic():
                _update_entity_ids(pk_to_entity_id)

            processed += len(rows)
            self.stdout.write(
                f'  processed {processed:,} / {total_remaining:,} '
                f'({processed * 100 // max(total_remaining, 1)}%) — '
                f'+{after_count - before_count} new GlobalEntity this batch'
            )

            if limit and processed >= limit:
                self.stdout.write(self.style.WARNING(f'Reached --limit={limit}, stopping.'))
                break

        remaining = _count_nulls()
        self.stdout.write(self.style.SUCCESS(
            f'Done. Backfilled {processed:,} rows, created {created_entities:,} GlobalEntity. '
            f'{remaining:,} still null (should be 0 unless --limit was used).'
        ))
