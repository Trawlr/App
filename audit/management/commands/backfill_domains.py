"""
Backfill domain entities from existing URL entities.

Usage:
    python manage.py backfill_domains
    python manage.py backfill_domains --dry-run
    python manage.py backfill_domains --batch-size=500
    python manage.py backfill_domains --cleanup  # Delete invalid domains first
"""
from urllib.parse import urlparse

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from audit.models import GlobalEntity, MessageEntity
from downloads.models import ArchivedMessage


class Command(BaseCommand):
    help = 'Backfill domain entities from existing URL entities'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be created without making changes',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=1000,
            help='Number of messages to process per batch (default: 1000)',
        )
        parser.add_argument(
            '--cleanup',
            action='store_true',
            help='Delete invalid domain entities (missing dot, contains spaces/asterisks)',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        batch_size = options['batch_size']
        cleanup = options['cleanup']

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN - no changes will be made'))

        # Cleanup invalid domain entities if requested
        if cleanup:
            self._cleanup_invalid_domains(dry_run)
            return

        # Run backfill
        self._backfill(dry_run, batch_size)

    def _cleanup_invalid_domains(self, dry_run: bool):
        """Delete domain entities that are invalid (no dot, contains spaces/asterisks)."""
        # Find invalid domains: missing dot, or contains space/asterisk
        invalid_domains = MessageEntity.objects.filter(
            entity__entity_type='domain'
        ).filter(
            Q(entity__text__contains=' ') |
            Q(entity__text__contains='*') |
            ~Q(entity__text__contains='.')
        )

        count = invalid_domains.count()
        self.stdout.write(f'Found {count} invalid domain entities')

        if count > 0:
            # Show some examples
            examples = list(invalid_domains.values_list('entity__text', flat=True)[:10])
            self.stdout.write(f'Examples: {examples}')

            if not dry_run:
                deleted, _ = invalid_domains.delete()
                self.stdout.write(self.style.SUCCESS(f'Deleted {deleted} invalid domain entities'))
            else:
                self.stdout.write(self.style.WARNING(f'DRY RUN: Would delete {count} invalid domains'))

    def _backfill(self, dry_run: bool, batch_size: int):
        """Create domain entities for messages that don't have them yet."""
        # Get messages that have URL entities but no domain entities yet
        messages_with_urls = MessageEntity.objects.filter(
            entity__entity_type__in=['url', 'text_url']
        ).exclude(
            entity__url=''
        ).values_list('message_id', flat=True).distinct()

        messages_with_domains = MessageEntity.objects.filter(
            entity__entity_type='domain'
        ).values_list('message_id', flat=True).distinct()

        # Find messages that need backfill
        messages_to_process = set(messages_with_urls) - set(messages_with_domains)

        total_messages = len(messages_to_process)
        self.stdout.write(f'Found {total_messages} messages needing domain backfill')

        if total_messages == 0:
            self.stdout.write(self.style.SUCCESS('Nothing to backfill'))
            return

        created_count = 0
        processed_count = 0
        message_list = list(messages_to_process)

        for i in range(0, total_messages, batch_size):
            batch_message_ids = message_list[i:i + batch_size]

            # Get URL entities for this batch — values come from the linked GlobalEntity
            url_entities = MessageEntity.objects.filter(
                message_id__in=batch_message_ids,
                entity__entity_type__in=['url', 'text_url']
            ).exclude(entity__url='').values('message_id', 'entity__url', 'offset')

            # Group by message and extract domains
            domain_dicts_per_message = {}

            for entity in url_entities:
                message_id = entity['message_id']
                url = entity['entity__url']
                offset = entity['offset']

                domain = self._extract_domain(url)
                if not domain:
                    continue

                if message_id not in domain_dicts_per_message:
                    domain_dicts_per_message[message_id] = {}

                if domain not in domain_dicts_per_message[message_id]:
                    domain_dicts_per_message[message_id][domain] = {
                        'url': url,
                        'offset': offset,
                    }

            # Look up channel_id for each message
            message_channel_map = dict(
                ArchivedMessage.objects.filter(
                    pk__in=domain_dicts_per_message.keys()
                ).values_list('pk', 'channel_id')
            )

            # Build entity dicts for dedup, then resolve to GlobalEntity ids in one batch
            all_entity_dicts = []
            for message_id, domains in domain_dicts_per_message.items():
                for domain, info in domains.items():
                    all_entity_dicts.append({
                        'entity_type': 'domain',
                        'text': domain,
                        'url': info['url'],
                        'user_id': None,
                        'custom_emoji_id': None,
                        'language': '',
                    })

            if not dry_run and all_entity_dicts:
                hash_to_id = GlobalEntity.bulk_get_or_create(all_entity_dicts)
            else:
                hash_to_id = {}

            domains_to_create = []
            for message_id, domains in domain_dicts_per_message.items():
                for domain, info in domains.items():
                    ed = {
                        'entity_type': 'domain', 'text': domain, 'url': info['url'],
                        'user_id': None, 'custom_emoji_id': None, 'language': '',
                    }
                    entity_id = hash_to_id.get(GlobalEntity.compute_hash(**ed)) if hash_to_id else None
                    if entity_id is None and not dry_run:
                        # Safety — shouldn't happen when not dry_run
                        continue
                    domains_to_create.append(MessageEntity(
                        message_id=message_id,
                        channel_id=message_channel_map.get(message_id),
                        entity_id=entity_id,
                        offset=info['offset'],
                        length=len(domain),
                    ))

            if domains_to_create and not dry_run:
                with transaction.atomic():
                    MessageEntity.objects.bulk_create(domains_to_create)

            created_count += len(domains_to_create)
            processed_count += len(batch_message_ids)

            self.stdout.write(
                f'Processed {processed_count}/{total_messages} messages, '
                f'created {created_count} domain entities'
            )

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'DRY RUN: Would create {created_count} domain entities'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Successfully created {created_count} domain entities'
            ))

    def _extract_domain(self, url: str) -> str:
        """Extract domain from URL, same logic as DomainParser."""
        # Clean markdown formatting that may have leaked into URL
        url = url.strip()
        url = url.lstrip('*_~`')
        url = url.rstrip('*_~`')

        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        try:
            parsed = urlparse(url)
            hostname = parsed.hostname or ''
            if hostname.startswith('www.'):
                hostname = hostname[4:]
            # Validate: must contain a dot and no invalid chars
            if '.' not in hostname or ' ' in hostname or '*' in hostname:
                return ''
            return hostname.lower()
        except Exception:
            return ''
