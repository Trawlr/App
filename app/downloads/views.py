"""
Downloads app views.
"""

import logging
import mimetypes
import os
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Case, Q, Sum, Value, When
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date
from django.utils.encoding import escape_uri_path
from django.views.decorators.http import require_http_methods

from silk.profiling.profiler import silk_profile

from accounts.models import TelegramAccount
from audit.models import TelegramChannel
from storage.utils import get_storage_backend

from .models import ArchivedMessage, DownloadedFile, DownloadTask

logger = logging.getLogger('trawlr.downloads')


# File browser sort options: (value, label)
FILE_SORT_CHOICES = [
    ('-downloaded_at', 'Newest first'),
    ('downloaded_at', 'Oldest first'),
    ('-file_size', 'Largest first'),
    ('file_size', 'Smallest first'),
    ('original_filename', 'Name (A-Z)'),
    ('-original_filename', 'Name (Z-A)'),
    ('file_type', 'Type'),
]
FILE_SORT_VALUES = {value for value, _ in FILE_SORT_CHOICES}

# File size buckets: (key, label, low_bytes_inclusive, high_bytes_exclusive_or_None)
FILE_SIZE_BUCKETS = [
    ('tiny', '< 1 MB', 0, 1024 * 1024),
    ('small', '1 - 10 MB', 1024 * 1024, 10 * 1024 * 1024),
    ('medium', '10 - 100 MB', 10 * 1024 * 1024, 100 * 1024 * 1024),
    ('large', '> 100 MB', 100 * 1024 * 1024, None),
]
FILE_SIZE_BUCKET_MAP = {key: (label, low, high) for key, label, low, high in FILE_SIZE_BUCKETS}


@login_required
@silk_profile(name='downloads.index')
def index(request):
    """Download queue with tabs (Active/Completed/Failed)."""
    tab = request.GET.get('tab', 'active')
    page = request.GET.get('page', 1)
    per_page = 25

    # Base queryset - only user's downloads
    base_qs = DownloadTask.objects.all().select_related('channel', 'channel__account')

    if tab == 'active':
        # Order by: downloading first, then by priority (desc), then by creation time
        downloads_qs = base_qs.filter(
            status__in=['pending', 'downloading']
        ).annotate(
            status_order=Case(
                When(status='downloading', then=Value(0)),
                default=Value(1),
            )
        ).order_by('status_order', '-priority', 'created_at')
    elif tab == 'completed':
        # Include 'unavailable' in completed since nothing more can be done
        downloads_qs = base_qs.filter(
            status__in=['completed', 'unavailable']
        ).order_by('-completed_at')
    elif tab == 'failed':
        downloads_qs = base_qs.filter(
            status='failed'
        ).order_by('-created_at')
    elif tab == 'paused':
        downloads_qs = base_qs.filter(
            status='paused'
        ).order_by('-created_at')
    elif tab == 'queue':
        # Queue management tab - no downloads list needed
        downloads_qs = DownloadTask.objects.none()
    else:
        downloads_qs = base_qs.order_by('-created_at')

    # Paginate results
    paginator = Paginator(downloads_qs, per_page)
    downloads = paginator.get_page(page)

    # Stats - include unavailable in completed count
    stats = {
        'active': base_qs.filter(status__in=['pending', 'downloading']).count(),
        'completed': base_qs.filter(status__in=['completed', 'unavailable']).count(),
        'failed': base_qs.filter(status='failed').count(),
        'paused': base_qs.filter(status='paused').count(),
    }

    context = {
        'downloads': downloads,
        'tab': tab,
        'stats': stats,
    }

    # Add queue stats for queue management tab
    if tab == 'queue':
        context['queue_stats'] = {
            'pending': DownloadTask.objects.filter(status='pending').count(),
            'downloading': DownloadTask.objects.filter(status='downloading').count(),
            'failed': DownloadTask.objects.filter(status='failed').count(),
            'paused': DownloadTask.objects.filter(status='paused').count(),
            'total': DownloadTask.objects.exclude(status='completed').count(),
        }

    return render(request, 'downloads/index.html', context)


@login_required
def queue(request):
    """Queue management (HTMX partial)."""
    tab = request.GET.get('tab', 'active')
    page = request.GET.get('page', 1)
    per_page = 25

    base_qs = DownloadTask.objects.all().select_related('channel', 'channel__account')

    if tab == 'active':
        # Order by: downloading first, then by priority (desc), then by creation time
        downloads_qs = base_qs.filter(
            status__in=['pending', 'downloading']
        ).annotate(
            status_order=Case(
                When(status='downloading', then=Value(0)),
                default=Value(1),
            )
        ).order_by('status_order', '-priority', 'created_at')
    elif tab == 'paused':
        downloads_qs = base_qs.filter(status='paused').order_by('-created_at')
    elif tab == 'completed':
        # Include 'unavailable' in completed since nothing more can be done
        downloads_qs = base_qs.filter(status__in=['completed', 'unavailable']).order_by('-completed_at')
    elif tab == 'failed':
        downloads_qs = base_qs.filter(status='failed').order_by('-created_at')
    else:
        downloads_qs = base_qs.order_by('-created_at')

    # Paginate results
    paginator = Paginator(downloads_qs, per_page)
    downloads = paginator.get_page(page)

    return render(request, 'downloads/partials/queue.html', {
        'downloads': downloads,
        'tab': tab,
    })


@login_required
@require_http_methods(['POST'])
def pause(request, pk):
    """Pause a download."""
    task = get_object_or_404(
        DownloadTask,
        pk=pk
    )

    if task.status in ['pending', 'downloading']:
        task.status = 'paused'
        task.save()
        messages.success(request, 'Download paused.')

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'status': 'paused'})
    return redirect('downloads:index')


@login_required
@require_http_methods(['POST'])
def resume(request, pk):
    """Resume a download."""
    task = get_object_or_404(
        DownloadTask,
        pk=pk
    )

    if task.status == 'paused':
        task.status = 'pending'
        task.save()
        messages.success(request, 'Download resumed.')

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'status': 'resumed'})
    return redirect('downloads:index')


@login_required
@require_http_methods(['POST'])
def cancel(request, pk):
    """Cancel a download."""
    task = get_object_or_404(
        DownloadTask,
        pk=pk
    )

    task.delete()
    messages.success(request, 'Download cancelled.')

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'status': 'cancelled'})
    return redirect('downloads:index')


@login_required
@require_http_methods(['POST'])
def retry(request, pk):
    """Retry a failed download."""
    task = get_object_or_404(
        DownloadTask,
        pk=pk
    )

    if task.status == 'failed':
        task.status = 'pending'
        task.retry_count = 0
        task.last_error = ''
        task.save()
        messages.success(request, 'Download queued for retry.')

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'status': 'retrying'})
    return redirect('downloads:index')


@login_required
@require_http_methods(['POST'])
def pause_all(request):
    """Pause all active downloads."""
    updated = DownloadTask.objects.filter(
        status__in=['pending', 'downloading']
    ).update(status='paused')

    messages.success(request, f'Paused {updated} downloads.')
    return redirect('downloads:index')


@login_required
@require_http_methods(['POST'])
def resume_all(request):
    """Resume all paused downloads."""
    updated = DownloadTask.objects.filter(
        status='paused'
    ).update(status='pending')

    messages.success(request, f'Resumed {updated} downloads.')
    return redirect('downloads:index')


@login_required
@require_http_methods(['POST'])
def clear_completed(request):
    """Clear all completed downloads from the queue."""
    deleted, _ = DownloadTask.objects.filter(
        status='completed'
    ).delete()

    messages.success(request, f'Cleared {deleted} completed downloads.')
    return redirect('downloads:index')


@login_required
@require_http_methods(['POST'])
def retry_all_failed(request):
    """Retry all failed downloads."""
    updated = DownloadTask.objects.filter(
        status='failed'
    ).update(status='pending', retry_count=0, last_error='')

    messages.success(request, f'Queued {updated} downloads for retry.')
    return redirect('downloads:index')


@login_required
@silk_profile(name='downloads.files')
def files(request):
    """Global file browser with pagination and filters."""
    file_type = request.GET.get('type', '')
    search = request.GET.get('search', '').strip()
    channel_id = request.GET.get('channel', '')
    account_id = request.GET.get('account', '')
    size_bucket = request.GET.get('size', '')
    after_raw = request.GET.get('after', '').strip()
    before_raw = request.GET.get('before', '').strip()
    sort = request.GET.get('sort', '-downloaded_at')
    if sort not in FILE_SORT_VALUES:
        sort = '-downloaded_at'
    page = request.GET.get('page', 1)

    files_qs = DownloadedFile.objects.filter(
        channel__account__is_active=True
    ).select_related('channel', 'channel__account', 'channel__config')

    if file_type:
        files_qs = files_qs.filter(file_type=file_type)

    if channel_id:
        files_qs = files_qs.filter(channel_id=channel_id)

    if account_id:
        files_qs = files_qs.filter(channel__account_id=account_id)

    if search:
        files_qs = files_qs.filter(
            Q(original_filename__icontains=search) |
            Q(channel__title__icontains=search)
        )

    if size_bucket in FILE_SIZE_BUCKET_MAP:
        _, low, high = FILE_SIZE_BUCKET_MAP[size_bucket]
        if low is not None:
            files_qs = files_qs.filter(file_size__gte=low)
        if high is not None:
            files_qs = files_qs.filter(file_size__lt=high)

    after_date = parse_date(after_raw) if after_raw else None
    before_date = parse_date(before_raw) if before_raw else None
    if after_date:
        files_qs = files_qs.filter(downloaded_at__date__gte=after_date)
    if before_date:
        files_qs = files_qs.filter(downloaded_at__date__lte=before_date)

    # Stable tiebreaker on pk so pagination doesn't shuffle equal-key rows.
    tiebreaker = '-pk' if sort.startswith('-') else 'pk'
    files_qs = files_qs.order_by(sort, tiebreaker)

    paginator = Paginator(files_qs, 48)
    files_page = paginator.get_page(page)

    total_storage = DownloadedFile.objects.filter(
        channel__account__is_active=True,
        is_duplicate=False
    ).aggregate(total=Sum('file_size'))['total'] or 0

    channels = TelegramChannel.objects.from_active_accounts().order_by('title')
    accounts = TelegramAccount.objects.active().order_by('display_name', 'phone_number')

    # Build active filter chips. For each chip, precompute a URL with that
    # filter removed (querystring tag can't take dynamic kwargs).
    def chip(key, label):
        qs = request.GET.copy()
        qs.pop(key, None)
        qs.pop('page', None)
        encoded = qs.urlencode()
        return {
            'key': key,
            'label': label,
            'remove_url': f'?{encoded}' if encoded else request.path,
        }

    active_filters = []
    if search:
        active_filters.append(chip('search', f'Search: "{search}"'))
    if channel_id:
        chan_obj = next((c for c in channels if str(c.pk) == str(channel_id)), None)
        active_filters.append(chip(
            'channel',
            f'Channel: {chan_obj.title if chan_obj else channel_id}',
        ))
    if account_id:
        acc_obj = next((a for a in accounts if str(a.pk) == str(account_id)), None)
        active_filters.append(chip(
            'account',
            f'Account: {acc_obj.name if acc_obj else account_id}',
        ))
    if size_bucket in FILE_SIZE_BUCKET_MAP:
        active_filters.append(chip(
            'size',
            f'Size: {FILE_SIZE_BUCKET_MAP[size_bucket][0]}',
        ))
    if after_date:
        active_filters.append(chip('after', f'Downloaded after: {after_date.isoformat()}'))
    if before_date:
        active_filters.append(chip('before', f'Downloaded before: {before_date.isoformat()}'))

    return render(request, 'downloads/files.html', {
        'files': files_page,
        'file_type': file_type,
        'search': search,
        'channel_id': channel_id,
        'account_id': account_id,
        'size_bucket': size_bucket,
        'after': after_raw,
        'before': before_raw,
        'sort': sort,
        'sort_choices': FILE_SORT_CHOICES,
        'size_buckets': FILE_SIZE_BUCKETS,
        'total_storage': total_storage,
        'channels': channels,
        'accounts': accounts,
        'active_filters': active_filters,
        'has_filters': bool(active_filters),
    })


@login_required
@silk_profile(name='downloads.file_detail')
def file_detail(request, pk):
    """File detail view."""
    file = get_object_or_404(
        DownloadedFile.objects.select_related('channel', 'original_file'),
        pk=pk
    )

    # Get all posts linked to this file (direct links)
    linked_posts = file.archived_messages.select_related('channel').all()

    # If this is an original file, also get posts from duplicate files
    duplicates = []
    duplicate_posts = []
    if not file.is_duplicate:
        duplicates = file.duplicates.select_related('channel').all()
        # Get posts linked to duplicate files
        from downloads.models import ArchivedMessage
        duplicate_file_ids = duplicates.values_list('pk', flat=True)
        duplicate_posts = ArchivedMessage.objects.filter(
            downloaded_file_id__in=duplicate_file_ids
        ).select_related('channel')

    return render(request, 'downloads/file_detail.html', {
        'file': file,
        'linked_posts': linked_posts,
        'duplicates': duplicates,
        'duplicate_posts': duplicate_posts,
    })


@login_required
def file_view(request, pk):
    """Modal viewer for images/videos (HTMX partial)."""
    file = get_object_or_404(
        DownloadedFile,
        pk=pk,
        channel__account__is_active=True
    )
    return render(request, 'downloads/partials/file_view.html', {'file': file})


@login_required
@require_http_methods(["POST"])
def file_delete(request, pk):
    """Force delete a file from disk (keeps database record with flag)."""
    file = get_object_or_404(
        DownloadedFile,
        pk=pk,
        channel__account__is_active=True
    )

    filename = file.original_filename

    # Delete the physical file if it exists and is not a duplicate
    backend = get_storage_backend()

    if not file.is_duplicate and file.file_path:
        try:
            backend.delete_file(file.file_path)
            logger.info(f"Deleted file from storage: {file.file_path}")
        except Exception as e:
            logger.error(f"Error deleting file from storage: {e}")

    # Delete thumbnail if exists (from DownloadedFile)
    if file.thumbnail_path:
        try:
            backend.delete_file(file.thumbnail_path)
            file.thumbnail_path = ''
        except Exception:
            pass

    # Also delete thumbnail from related ArchivedMessage if it exists
    archived_msg = ArchivedMessage.objects.filter(
        channel=file.channel,
        message_id=file.message_id
    ).first()
    if archived_msg and archived_msg.thumbnail_path:
        try:
            backend.delete_file(archived_msg.thumbnail_path)
            logger.info(f"Deleted ArchivedMessage thumbnail: {archived_msg.thumbnail_path}")
            archived_msg.thumbnail_path = ''
            archived_msg.save(update_fields=['thumbnail_path'])
        except Exception as e:
            logger.debug(f"Error deleting ArchivedMessage thumbnail: {e}")

    # Mark as deleted from disk (keep database record)
    file.deleted_from_disk = True
    file.save(update_fields=['deleted_from_disk', 'thumbnail_path'])

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'message': f'Deleted {filename} from disk'
        })

    messages.success(request, f'Deleted {filename} from disk')
    return redirect('downloads:file_detail', pk=pk)


@login_required
def serve_media(request, pk):
    """Serve a downloaded file from the storage backend with range request support."""

    file_record = get_object_or_404(
        DownloadedFile,
        pk=pk
    )

    if file_record.deleted_from_disk:
        raise Http404("File has been deleted")

    # If it's a duplicate, serve the original file
    if file_record.is_duplicate and file_record.original_file:
        file_record = file_record.original_file

    if not file_record.file_path:
        raise Http404("No file path")

    # Determine content type from file extension (more reliable than stored mime type)
    # Telegram sometimes reports wrong mime types (e.g., video/quicktime for .mp4 files)
    content_type, _ = mimetypes.guess_type(file_record.file_path)
    if not content_type:
        content_type = 'application/octet-stream'

    # Force correct video mime types for browser compatibility
    file_ext = Path(file_record.file_path).suffix.lower()
    if file_ext == '.mp4':
        content_type = 'video/mp4'
    elif file_ext == '.webm':
        content_type = 'video/webm'
    elif file_ext == '.mov':
        content_type = 'video/mp4'  # Most .mov files play as mp4 in browsers

    backend = get_storage_backend()
    return backend.get_serve_response(
        file_record.file_path,
        content_type,
        filename=file_record.original_filename,
    )


@login_required
def serve_thumbnail(request, pk):
    """Serve a thumbnail for a downloaded file from the storage backend."""

    file_record = get_object_or_404(
        DownloadedFile,
        pk=pk
    )

    if file_record.deleted_from_disk:
        raise Http404("File has been deleted")

    # If it's a duplicate, use the original file's thumbnail
    if file_record.is_duplicate and file_record.original_file:
        file_record = file_record.original_file

    if not file_record.thumbnail_path:
        raise Http404("No thumbnail available")

    # Determine content type
    content_type, _ = mimetypes.guess_type(file_record.thumbnail_path)
    if not content_type:
        content_type = 'image/jpeg'

    backend = get_storage_backend()
    return backend.get_serve_response(
        file_record.thumbnail_path,
        content_type,
    )


@login_required
@require_http_methods(["POST"])
def archived_message_delete_thumbnail(request, pk):
    """Delete just the thumbnail from an archived message."""
    archived_msg = get_object_or_404(
        ArchivedMessage.objects.from_active_accounts().select_related('channel', 'channel__account'),
        pk=pk
    )

    if not archived_msg.thumbnail_path:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'message': 'No thumbnail to delete'
            })
        messages.warning(request, 'No thumbnail to delete')
        return redirect(request.META.get('HTTP_REFERER', 'audit:messages'))

    # Delete the physical thumbnail file
    try:
        backend = get_storage_backend()
        backend.delete_file(archived_msg.thumbnail_path)
        logger.info(f"Deleted ArchivedMessage thumbnail: {archived_msg.thumbnail_path}")
    except Exception as e:
        logger.error(f"Error deleting ArchivedMessage thumbnail: {e}")

    # Clear the thumbnail_path field
    archived_msg.thumbnail_path = ''
    archived_msg.save(update_fields=['thumbnail_path'])

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'message': 'Thumbnail deleted'
        })

    messages.success(request, 'Thumbnail deleted')
    return redirect(request.META.get('HTTP_REFERER', 'audit:messages'))


