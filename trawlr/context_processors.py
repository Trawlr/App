"""
Custom context processors for Trawlr.
"""

from django.db.models import Count, Q, Sum

from accounts.models import TelegramAccount
from audit.models import UserMonitoredSource
from downloads.models import DownloadedFile, DownloadTask
from trawlr.version import get_build_number


def sidebar_context(request):
    """Add sidebar data to all templates."""
    if not request.user.is_authenticated:
        return {}

    # Calculate storage used
    storage_used = DownloadedFile.objects.filter(
        channel__account__user=request.user,
        is_duplicate=False
    ).aggregate(total=Sum('file_size'))['total'] or 0

    # Active and failed download counts in a single query
    download_counts = DownloadTask.objects.filter(
        channel__account__user=request.user,
    ).aggregate(
        active=Count('id', filter=Q(status__in=['pending', 'downloading'])),
        failed=Count('id', filter=Q(status='failed')),
    )

    # Pinned/monitored sources for sidebar (user-specific)
    pinned_sources = UserMonitoredSource.objects.filter(
        user=request.user
    ).select_related('channel').order_by('channel__title')

    # Active and authenticated telegram accounts for join channel modal
    accounts = TelegramAccount.objects.filter(
        user=request.user,
        is_active=True,
        is_authenticated=True
    ).only('id', 'display_name', 'phone_number', 'is_authenticated')

    return {
        'sidebar_storage_bytes': storage_used,
        'sidebar_active_downloads': download_counts['active'],
        'sidebar_failed_downloads': download_counts['failed'],
        'sidebar_pinned': pinned_sources,
        'accounts': accounts,
        'build_number': get_build_number(),
    }
