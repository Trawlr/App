"""
Initial setup command for Trawlr.

This command should be run once during initial deployment to
1. Enable the required PostgreSQL extensions
2. Create an initial admin account

Usage:
    python manage.py setup
    python manage.py setup --username admin --email admin@example.com --password secret
"""
import os
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import connection


User = get_user_model()

class Command(BaseCommand):
    help = 'Initial provisioning: verify PostgreSQL extensions and create superuser'

    def add_arguments(self, parser):
        parser.add_argument(
            '--username',
            help='Admin username',
        )
        parser.add_argument(
            '--email',
            help='Admin user email',
        )
        parser.add_argument(
            '--password',
            help='Admin account password',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING('Trawlr Setup'))
        self.stdout.write('')
        self._check_postgres_extensions()
        self._create_adminuser(options)
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Trawlr setup complete.'))

    def _check_postgres_extensions(self):
        """Verify required PostgreSQL extensions are available."""
        self.stdout.write('Checking PostgreSQL extensions')
        required_extensions = ['pg_trgm']
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT extname FROM pg_extension WHERE extname = ANY(%s)",
                [required_extensions]
            )
            installed = {row[0] for row in cursor.fetchall()}

        all_present = True
        for ext in required_extensions:
            if ext in installed:
                self.stdout.write(f'  {ext}: ' + self.style.SUCCESS('OK'))
            else:
                self.stdout.write(f'  {ext}: ' + self.style.ERROR('MISSING'))
                all_present = False

        if not all_present:
            self.stdout.write(self.style.WARNING(
                '\nPostgres extensions are missing. Run the following SQL query manually:'
            ))
            for ext in required_extensions:
                if ext not in installed:
                    self.stdout.write(f'  CREATE EXTENSION IF NOT EXISTS {ext};')
            self.stdout.write('')

    def _create_adminuser(self, options):
        """Create a new django super user"""
        self.stdout.write('Creating admin account')
        username = options.get('username')
        email = options.get('email')
        password = options.get('password')

        if not username or not password:
            self.stdout.write(self.style.WARNING(
                '  No credentials provided. Ensure you are using the correct cli arguments'
            ))
            return

        User.objects.create_superuser(
            username=username,
            email=email,
            password=password,
        )
        self.stdout.write(f'  User {username}: ' + self.style.SUCCESS('OK'))        
