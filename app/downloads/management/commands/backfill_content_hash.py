"""
Backfill ArchivedMessage.content_hash for messages ingested before the field
existed. content_hash powers the CIB burst-cluster detector — without it, text
clustering across channels is impossible to query efficiently.

Idempotent: filters to rows with content_hash IS NULL and non-empty text.
Existing values are never overwritten. Safe to re-run.
"""

import time

from django.core.management.base import BaseCommand

from downloads.models import ArchivedMessage
from events.processor import compute_content_hash


PROGRESS_EVERY = 1000
BULK_UPDATE_BATCH = 500


class Command(BaseCommand):
    help = 'Backfill ArchivedMessage.content_hash from existing message text'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would be updated without writing')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        t0 = time.monotonic()

        # Only rows with non-empty text and no existing hash. Excludes rows where
        # text is null or empty (those will hash to None anyway and add no signal).
        qs = (
            ArchivedMessage.objects
            .filter(content_hash__isnull=True)
            .exclude(text='')
            .only('id', 'text')
        )

        total = qs.count()
        self.stdout.write(f'Scanning {total:,} messages with empty content_hash...')
        if total == 0:
            self.stdout.write(self.style.WARNING('Nothing to backfill.'))
            return

        to_update = []
        updated = 0
        skipped_short = 0
        scanned = 0

        for msg in qs.iterator(chunk_size=2000):
            scanned += 1
            h = compute_content_hash(msg.text)
            if h is None:
                # Text was too short after normalization — nothing to store.
                skipped_short += 1
            else:
                msg.content_hash = h
                to_update.append(msg)

            if len(to_update) >= BULK_UPDATE_BATCH:
                if not dry_run:
                    ArchivedMessage.objects.bulk_update(
                        to_update, fields=['content_hash'], batch_size=BULK_UPDATE_BATCH,
                    )
                updated += len(to_update)
                to_update = []

            if scanned % PROGRESS_EVERY == 0:
                rate = scanned / max(time.monotonic() - t0, 1e-6)
                self.stdout.write(
                    f'  scanned {scanned:,}/{total:,} '
                    f'({updated:,} hashed, {skipped_short:,} too-short, {rate:,.0f}/s)'
                )

        # Flush remainder
        if to_update:
            if not dry_run:
                ArchivedMessage.objects.bulk_update(
                    to_update, fields=['content_hash'], batch_size=BULK_UPDATE_BATCH,
                )
            updated += len(to_update)

        elapsed = time.monotonic() - t0
        prefix = 'DRY RUN — would set' if dry_run else 'Set'
        self.stdout.write(self.style.SUCCESS(
            f'{prefix} content_hash on {updated:,} messages in {elapsed:.1f}s '
            f'(skipped {skipped_short:,} too-short)'
        ))
