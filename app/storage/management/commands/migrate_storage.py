"""
Management command to migrate existing local files to the configured cloud storage backend.
"""

import os
from pathlib import Path

from django.core.management.base import BaseCommand

from accounts.models import GlobalSettings
from downloads.models import ArchivedMessage, DownloadedFile
from storage.backends import LocalStorageBackend
from storage.utils import get_storage_backend, is_cloud_backend


class Command(BaseCommand):
    help = 'Migrate existing local files to the configured cloud storage backend'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview what would be migrated without actually uploading',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=100,
            help='Number of records to process per batch (default: 100)',
        )
        parser.add_argument(
            '--delete-local',
            action='store_true',
            help='Delete local files after successful upload to cloud storage',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        batch_size = options['batch_size']
        delete_local = options['delete_local']

        settings = GlobalSettings.get_settings()

        if not is_cloud_backend(settings):
            self.stderr.write(self.style.ERROR(
                f'Storage provider is "{settings.storage_provider}". '
                'This command migrates from local to cloud storage (S3 or Azure). '
                'Configure a cloud storage provider in Settings first.'
            ))
            return

        storage_root = Path(settings.storage_root)
        if not storage_root.exists():
            self.stderr.write(self.style.ERROR(
                f'Local storage root does not exist: {storage_root}'
            ))
            return

        # Create a local backend to read from, and the cloud backend to write to
        local_backend = LocalStorageBackend(storage_root=str(storage_root))
        cloud_backend = get_storage_backend(settings)

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN - no files will be uploaded'))

        self.stdout.write(f'Migrating from local ({storage_root}) to {settings.storage_provider}')
        self.stdout.write('')

        # Phase 1: Migrate downloaded files
        files_total = DownloadedFile.objects.filter(
            file_path__gt='',
            is_duplicate=False,
            deleted_from_disk=False,
        ).count()
        self.stdout.write(f'Found {files_total} downloaded files to check')

        files_migrated = 0
        files_skipped = 0
        files_missing = 0
        files_errors = 0

        queryset = DownloadedFile.objects.filter(
            file_path__gt='',
            is_duplicate=False,
            deleted_from_disk=False,
        ).order_by('pk')

        for df in queryset.iterator(chunk_size=batch_size):
            local_path = storage_root / df.file_path
            if not local_path.exists():
                files_missing += 1
                continue

            # Check if already exists in cloud
            if cloud_backend.file_exists(df.file_path):
                files_skipped += 1
                continue

            if dry_run:
                files_migrated += 1
                if files_migrated <= 10:
                    self.stdout.write(f'  Would upload: {df.file_path}')
                continue

            try:
                cloud_backend.save_file(df.file_path, str(local_path))
                files_migrated += 1
                if delete_local:
                    os.remove(local_path)
                if files_migrated % 50 == 0:
                    self.stdout.write(f'  Uploaded {files_migrated} files...')
            except Exception as e:
                files_errors += 1
                self.stderr.write(f'  Error uploading {df.file_path}: {e}')

        # Also migrate thumbnails from DownloadedFile
        thumb_files = DownloadedFile.objects.filter(
            thumbnail_path__gt='',
            deleted_from_disk=False,
        ).values_list('thumbnail_path', flat=True).distinct()

        # Phase 2: Migrate thumbnails from ArchivedMessages
        thumb_messages = ArchivedMessage.objects.filter(
            thumbnail_path__gt='',
        ).values_list('thumbnail_path', flat=True).distinct()

        # Combine all thumbnail paths
        all_thumbnails = set(thumb_files) | set(thumb_messages)
        self.stdout.write(f'Found {len(all_thumbnails)} unique thumbnail paths to check')

        thumbs_migrated = 0
        thumbs_skipped = 0
        thumbs_missing = 0
        thumbs_errors = 0

        for thumb_path in all_thumbnails:
            local_path = storage_root / thumb_path
            if not local_path.exists():
                thumbs_missing += 1
                continue

            if cloud_backend.file_exists(thumb_path):
                thumbs_skipped += 1
                continue

            if dry_run:
                thumbs_migrated += 1
                if thumbs_migrated <= 10:
                    self.stdout.write(f'  Would upload thumbnail: {thumb_path}')
                continue

            try:
                cloud_backend.save_file(thumb_path, str(local_path))
                thumbs_migrated += 1
                if delete_local:
                    os.remove(local_path)
                if thumbs_migrated % 100 == 0:
                    self.stdout.write(f'  Uploaded {thumbs_migrated} thumbnails...')
            except Exception as e:
                thumbs_errors += 1
                self.stderr.write(f'  Error uploading thumbnail {thumb_path}: {e}')

        # Summary
        self.stdout.write('')
        action = 'Would migrate' if dry_run else 'Migrated'
        self.stdout.write(self.style.SUCCESS(f'{action}:'))
        self.stdout.write(f'  Files:      {files_migrated} uploaded, {files_skipped} already exist, '
                         f'{files_missing} missing from disk, {files_errors} errors')
        self.stdout.write(f'  Thumbnails: {thumbs_migrated} uploaded, {thumbs_skipped} already exist, '
                         f'{thumbs_missing} missing from disk, {thumbs_errors} errors')

        total = files_migrated + thumbs_migrated
        if dry_run:
            self.stdout.write(self.style.WARNING(f'\nDry run complete. {total} files would be uploaded.'))
        else:
            self.stdout.write(self.style.SUCCESS(f'\nMigration complete. {total} files uploaded.'))
