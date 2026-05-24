"""
Usage:
    python manage.py notification_test --entry <id>
"""

from django.core.management.base import BaseCommand, CommandError

from notifications.matcher import build_test_delivery
from notifications.models import WatchlistEntry
from notifications.tasks import deliver_notification


class Command(BaseCommand):
    help = 'Queue a synthetic delivery against a watchlist entry to validate config.'

    def add_arguments(self, parser):
        parser.add_argument('--entry', type=int, required=True,
                            help='WatchlistEntry primary key.')
        parser.add_argument('--inline', action='store_true',
                            help='Deliver synchronously in this process (skip the queue).')

    def handle(self, *args, **opts):
        try:
            entry = WatchlistEntry.objects.get(pk=opts['entry'])
        except WatchlistEntry.DoesNotExist:
            raise CommandError(f"WatchlistEntry {opts['entry']} not found")

        delivery = build_test_delivery(entry)
        self.stdout.write(f"Created NotificationDelivery {delivery.pk} for entry {entry.pk} ({entry.name}).")

        if opts['inline']:
            self.stdout.write("Delivering inline (no broker)...")
            deliver_notification(delivery.pk)
        else:
            deliver_notification.send(delivery.pk)
            self.stdout.write("Dispatched to broker. Watch worker logs for delivery.")
