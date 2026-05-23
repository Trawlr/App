"""
Management command to link existing DownloadedFile records to ArchivedMessage records.
This fixes any ArchivedMessage records where downloaded_file is NULL but a DownloadedFile exists.
"""

from django.core.management.base import BaseCommand

from downloads.models import ArchivedMessage, DownloadedFile


class Command(BaseCommand):
    help = 'Link existing DownloadedFile records to their corresponding ArchivedMessage records'

    def handle(self, *args, **options):
        # Get all DownloadedFiles
        downloaded_files = DownloadedFile.objects.all()
        updated_count = 0

        for df in downloaded_files:
            # Update any ArchivedMessage with matching channel and message_id
            updated = ArchivedMessage.objects.filter(
                channel=df.channel,
                message_id=df.message_id,
                downloaded_file__isnull=True
            ).update(downloaded_file=df)

            if updated:
                updated_count += updated
                self.stdout.write(f"Linked message {df.message_id} in {df.channel.title}")

        self.stdout.write(
            self.style.SUCCESS(f'Successfully linked {updated_count} ArchivedMessage records')
        )
