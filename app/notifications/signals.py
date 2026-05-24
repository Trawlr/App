"""
Invalidate the matcher's process-local watchlist snapshot whenever an entry
changes. The 30s TTL is the upper bound; this drops it to next-message for
the web process.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .matcher import invalidate_cache
from .models import WatchlistEntry


@receiver(post_save, sender=WatchlistEntry)
@receiver(post_delete, sender=WatchlistEntry)
def _invalidate_on_change(sender, **kwargs):
    invalidate_cache()
