"""
Management command to backfill topic information for existing messages.

For forum channels, updates messages that have reply_to_message_id set
to properly mark them as topic messages and link them to ForumTopic records.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from audit.models import TelegramChannel, ForumTopic
from downloads.models import ArchivedMessage


class Command(BaseCommand):
    help = 'Backfill topic information for existing messages in forum channels'

    def add_arguments(self, parser):
        parser.add_argument(
            '--channel',
            type=int,
            help='Only process a specific channel (by pk)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be updated without making changes',
        )
        parser.add_argument(
            '--sync-topics',
            action='store_true',
            help='Queue topic sync tasks to fetch actual topic names from Telegram',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        sync_topics = options['sync_topics']
        channel_pk = options.get('channel')

        # Get forum channels
        channels_qs = TelegramChannel.objects.filter(is_forum=True)
        if channel_pk:
            channels_qs = channels_qs.filter(pk=channel_pk)

        forum_channels = list(channels_qs)

        if not forum_channels:
            self.stdout.write(self.style.WARNING('No forum channels found'))
            return

        self.stdout.write(f'Found {len(forum_channels)} forum channel(s)')

        total_updated = 0
        total_topics_created = 0
        channels_to_sync = set()

        for channel in forum_channels:
            self.stdout.write(f'\nProcessing: {channel.title}')

            # Find messages with reply_to_message_id that aren't already marked as topic messages
            messages = ArchivedMessage.objects.filter(
                channel=channel,
                reply_to_message_id__isnull=False,
                is_topic_message=False,
            )

            count = messages.count()
            if count == 0:
                self.stdout.write(f'  No messages to update')
                continue

            self.stdout.write(f'  Found {count} messages to process')

            if dry_run:
                # Just count unique topic IDs
                topic_ids = set(messages.values_list('reply_to_message_id', flat=True))
                self.stdout.write(f'  Would create/link {len(topic_ids)} topic(s)')
                self.stdout.write(f'  Would update {count} message(s)')
                total_updated += count
                total_topics_created += len(topic_ids)
                continue

            # Process in batches
            batch_size = 1000
            updated_in_channel = 0
            topics_in_channel = 0

            # First, get all unique topic IDs for this channel
            topic_ids = set(messages.values_list('reply_to_message_id', flat=True))

            # Create ForumTopic records for each unique topic ID
            for topic_id in topic_ids:
                topic, created = ForumTopic.objects.get_or_create(
                    channel=channel,
                    topic_id=topic_id,
                    defaults={
                        'title': 'General' if topic_id == 1 else f'Topic {topic_id}',
                        'is_general': topic_id == 1,
                    }
                )
                if created:
                    topics_in_channel += 1
                    self.stdout.write(f'    Created topic: {topic.title}')

            # Now update all messages with their topic links
            for topic_id in topic_ids:
                topic = ForumTopic.objects.get(channel=channel, topic_id=topic_id)

                with transaction.atomic():
                    updated = ArchivedMessage.objects.filter(
                        channel=channel,
                        reply_to_message_id=topic_id,
                        is_topic_message=False,
                    ).update(
                        is_topic_message=True,
                        reply_to_top_id=topic_id,
                        reply_to_message_id=None,  # Clear this since it's not a reply
                        topic=topic,
                    )
                    updated_in_channel += updated

            self.stdout.write(f'  Created {topics_in_channel} topic(s)')
            self.stdout.write(f'  Updated {updated_in_channel} message(s)')

            total_updated += updated_in_channel
            total_topics_created += topics_in_channel

            if topics_in_channel > 0:
                channels_to_sync.add(channel.pk)

        # Summary
        self.stdout.write('')
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'DRY RUN: Would update {total_updated} messages and create {total_topics_created} topics'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Updated {total_updated} messages and created {total_topics_created} topics'
            ))

            # Queue topic sync if requested
            if sync_topics and channels_to_sync:
                from tasks import sync_forum_topics

                self.stdout.write(f'\nQueuing topic sync for {len(channels_to_sync)} channel(s)...')
                for channel_pk in channels_to_sync:
                    sync_forum_topics.send(channel_pk)
                    self.stdout.write(f'  Queued sync for channel {channel_pk}')

                self.stdout.write(self.style.SUCCESS(
                    'Topic sync tasks queued. Run workers to fetch actual topic names.'
                ))
