"""
Report views and API endpoints.
"""

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, redirect
from silk.profiling.profiler import silk_profile

from accounts.models import TelegramAccount

from . import queries, exports


# =============================================================================
# Helper Functions
# =============================================================================

def _get_date_context(request):
    """Get common date range context for templates."""
    start_date, end_date, days_label = queries.parse_date_range(request)
    return {
        'start_date': start_date,
        'end_date': end_date,
        'days_label': days_label,
        'date_presets': queries.DATE_PRESETS,
    }


# =============================================================================
# HTML Report Views
# =============================================================================

@login_required
def index(request):
    """Reports index - redirect to content analytics."""
    return redirect('reports:content')


@login_required
@silk_profile(name='reports.content_analytics')
def content_analytics(request):
    """Content Analytics dashboard."""
    ctx = _get_date_context(request)
    start_date, end_date = ctx['start_date'], ctx['end_date']

    # Get summary stats for initial render
    ctx['stats'] = queries.get_engagement_stats(start_date, end_date)
    ctx['media_distribution'] = list(queries.get_media_distribution(start_date, end_date))

    return render(request, 'reports/content.html', ctx)


@login_required
@silk_profile(name='reports.user_reports')
def user_reports(request):
    """User dashboard."""
    ctx = _get_date_context(request)
    start_date, end_date = ctx['start_date'], ctx['end_date']

    # Get summary stats
    ctx['stats'] = queries.get_user_activity_stats(start_date, end_date)
    ctx['churn'] = queries.get_user_churn(start_date, end_date)
    ctx['flagged_users'] = queries.get_flagged_users()[:10]
    ctx['suspicious_users'] = queries.get_suspicious_users()[:10]
    ctx['top_posters'] = queries.get_top_posters(10)
    ctx['cross_channel_users'] = queries.get_cross_channel_users(10)

    return render(request, 'reports/users.html', ctx)


@login_required
@silk_profile(name='reports.source_analytics')
def source_analytics(request):
    """Source Analytics dashboard."""
    ctx = _get_date_context(request)
    start_date, end_date = ctx['start_date'], ctx['end_date']

    # Get summary stats
    ctx['stats'] = queries.get_source_stats(start_date, end_date)
    ctx['channel_health'] = list(queries.get_channel_health())
    ctx['channel_types'] = list(queries.get_channel_type_breakdown())
    ctx['download_progress'] = queries.get_download_progress()
    ctx['forum_activity'] = list(queries.get_forum_activity())[:10]
    ctx['multi_account_channels'] = queries.get_multi_account_channels()[:10]

    return render(request, 'reports/sources.html', ctx)


@login_required
@silk_profile(name='reports.media_inventory')
def media_inventory(request):
    """Per-source media inventory: counts and bytes broken down by file type."""
    ctx = _get_date_context(request)

    media_type = request.GET.get('media_type') or None
    if media_type not in queries.MEDIA_TYPES:
        media_type = None

    size_band = request.GET.get('size_band') or None
    if size_band not in queries.SIZE_BANDS:
        size_band = None

    account_id = request.GET.get('account') or ''
    account_id_int = int(account_id) if account_id.isdigit() else None

    rows = queries.get_source_media_inventory(
        media_type=media_type,
        size_band=size_band,
        account_id=account_id_int,
    )
    totals = queries.summarize_inventory_totals(rows, media_type=media_type)

    ctx.update({
        'rows': rows,
        'totals': totals,
        'media_type': media_type or '',
        'size_band': size_band or '',
        'account_id': account_id,
        'size_band_choices': [
            (key, queries.SIZE_BAND_LABELS[key]) for key in queries.SIZE_BANDS
        ],
        'accounts': TelegramAccount.objects.active().order_by('display_name', 'phone_number'),
    })
    return render(request, 'reports/media_inventory.html', ctx)


@login_required
@silk_profile(name='reports.investigation')
def investigation(request):
    """Investigation dashboard."""
    ctx = _get_date_context(request)
    start_date, end_date = ctx['start_date'], ctx['end_date']

    # Get summary stats
    ctx['report_stats'] = queries.get_report_stats(start_date, end_date)
    ctx['exclusion_stats'] = queries.get_exclusion_stats()
    ctx['notes_count'] = queries.get_user_notes_count(start_date, end_date)

    return render(request, 'reports/investigation.html', ctx)


# =============================================================================
# API Endpoints (JSON for Charts)
# =============================================================================

@login_required
@silk_profile(name='reports.api_content_volume')
def api_content_volume(request):
    """API: Message volume by day."""
    start_date, end_date, _ = queries.parse_date_range(request)
    data = queries.get_message_volume(start_date, end_date)

    labels = []
    values = []
    for item in data:
        labels.append(item['date'].strftime('%b %d'))
        values.append(item['count'])

    return JsonResponse({
        'labels': labels,
        'values': values,
    })


@login_required
@silk_profile(name='reports.api_content_media')
def api_content_media(request):
    """API: Media type distribution."""
    start_date, end_date, _ = queries.parse_date_range(request)
    data = queries.get_media_distribution(start_date, end_date)

    labels = []
    values = []
    colors = []

    color_map = {
        'photo': '#0dcaf0',  # Cyan
        'video': '#198754',  # Green
        'file': '#ffc107',   # Yellow
    }

    for item in data:
        labels.append(item['media_type'].title())
        values.append(item['count'])
        colors.append(color_map.get(item['media_type'], '#6c757d'))

    return JsonResponse({
        'labels': labels,
        'values': values,
        'colors': colors,
    })


@login_required
@silk_profile(name='reports.api_content_entities')
def api_content_entities(request):
    """API: Top entities (URLs, hashtags, mentions)."""
    start_date, end_date, _ = queries.parse_date_range(request)
    entity_type = request.GET.get('type', 'hashtag')

    if entity_type == 'url':
        data = queries.get_top_urls(start_date, end_date, limit=10)
        labels = [item['url'][:50] + '...' if len(item['url']) > 50 else item['url'] for item in data]
    else:
        data = queries.get_top_entities(start_date, end_date, entity_type, limit=10)
        labels = [item['text'] for item in data]

    values = [item['count'] for item in data]

    return JsonResponse({
        'labels': labels,
        'values': values,
    })


@login_required
@silk_profile(name='reports.api_content_forwards')
def api_content_forwards(request):
    """API: Top forward sources."""
    start_date, end_date, _ = queries.parse_date_range(request)
    data = queries.get_top_forward_sources(start_date, end_date, limit=10)

    labels = [item['source_title'][:30] for item in data]
    values = [item['count'] for item in data]

    return JsonResponse({
        'labels': labels,
        'values': values,
    })


@login_required
@silk_profile(name='reports.api_users_activity')
def api_users_activity(request):
    """API: New users by day."""
    start_date, end_date, _ = queries.parse_date_range(request)
    data = queries.get_new_users_by_day(start_date, end_date)

    labels = []
    values = []
    for item in data:
        labels.append(item['date'].strftime('%b %d'))
        values.append(item['count'])

    return JsonResponse({
        'labels': labels,
        'values': values,
    })


@login_required
@silk_profile(name='reports.api_users_churn')
def api_users_churn(request):
    """API: User churn data."""
    start_date, end_date, _ = queries.parse_date_range(request)
    data = queries.get_user_churn(start_date, end_date)

    return JsonResponse(data)


@login_required
@silk_profile(name='reports.api_users_top_posters')
def api_users_top_posters(request):
    """API: Top posters chart data."""
    data = queries.get_top_posters(10)

    labels = []
    values = []
    for user in data:
        name = user.username or f"{user.first_name} {user.last_name}".strip() or str(user.telegram_id)
        labels.append(name[:20])
        values.append(user.message_count)

    return JsonResponse({
        'labels': labels,
        'values': values,
    })


@login_required
@silk_profile(name='reports.api_sources_health')
def api_sources_health(request):
    """API: Channel health status distribution."""
    data = queries.get_channel_health()

    labels = []
    values = []
    colors = []

    color_map = {
        'active': '#198754',      # Green
        'unavailable': '#dc3545', # Red
        'deleted': '#6c757d',     # Gray
        'restricted': '#ffc107',  # Yellow
        'private': '#0dcaf0',     # Cyan
        'unknown': '#adb5bd',     # Light gray
    }

    for item in data:
        labels.append(item['availability_status'].title())
        values.append(item['count'])
        colors.append(color_map.get(item['availability_status'], '#6c757d'))

    return JsonResponse({
        'labels': labels,
        'values': values,
        'colors': colors,
    })


@login_required
@silk_profile(name='reports.api_sources_volume')
def api_sources_volume(request):
    """API: Content volume by source."""
    start_date, end_date, _ = queries.parse_date_range(request)
    data = queries.get_content_volume_by_source(start_date, end_date, limit=10)

    labels = [item['channel__title'][:25] for item in data]
    values = [item['count'] for item in data]

    return JsonResponse({
        'labels': labels,
        'values': values,
    })


@login_required
@silk_profile(name='reports.api_media_inventory_trend')
def api_media_inventory_trend(request):
    """API: Storage growth (top N sources) over the selected date range."""
    start_date, end_date, _ = queries.parse_date_range(request)
    account_id = request.GET.get('account') or ''
    account_id_int = int(account_id) if account_id.isdigit() else None

    data = queries.get_source_storage_trend(
        start_date, end_date, top_n=10, account_id=account_id_int,
    )

    labels = [d.strftime('%b %d') for d in data['dates']]
    datasets = []
    for ch in data['channels']:
        datasets.append({
            'channel_id': ch['id'],
            'label': ch['title'] or ch['username'] or f"Source {ch['id']}",
            'daily_bytes': data['series'][ch['id']],
        })

    return JsonResponse({
        'labels': labels,
        'datasets': datasets,
    })


@login_required
@silk_profile(name='reports.api_investigation_reports')
def api_investigation_reports(request):
    """API: Report submission stats."""
    start_date, end_date, _ = queries.parse_date_range(request)
    data = queries.get_report_stats(start_date, end_date)

    return JsonResponse({
        'success_rate': {
            'labels': ['Successful', 'Failed'],
            'values': [data['successful'], data['failed']],
            'colors': ['#198754', '#dc3545'],
        },
        'by_type': {
            'labels': [item['report_type'].title() for item in data['by_type']],
            'values': [item['count'] for item in data['by_type']],
        },
        'by_reason': {
            'labels': [item['reason'].replace('_', ' ').title() for item in data['by_reason']],
            'values': [item['count'] for item in data['by_reason']],
        },
    })


# =============================================================================
# Export Endpoints
# =============================================================================

@login_required
def export_content(request):
    """Export content analytics."""
    start_date, end_date, days_label = queries.parse_date_range(request)
    format_type = request.GET.get('format', 'csv')

    if format_type == 'json':
        return exports.export_content_json(start_date, end_date, days_label)
    return exports.export_content_csv(start_date, end_date, days_label)


@login_required
def export_users(request):
    """Export user intelligence."""
    start_date, end_date, days_label = queries.parse_date_range(request)
    format_type = request.GET.get('format', 'csv')

    if format_type == 'json':
        return exports.export_users_json(start_date, end_date, days_label)
    return exports.export_users_csv(start_date, end_date, days_label)


@login_required
def export_sources(request):
    """Export source analytics."""
    start_date, end_date, days_label = queries.parse_date_range(request)
    format_type = request.GET.get('format', 'csv')

    if format_type == 'json':
        return exports.export_sources_json(start_date, end_date, days_label)
    return exports.export_sources_csv(start_date, end_date, days_label)


@login_required
def export_investigation(request):
    """Export investigation report."""
    start_date, end_date, days_label = queries.parse_date_range(request)
    format_type = request.GET.get('format', 'csv')

    if format_type == 'json':
        return exports.export_investigation_json(start_date, end_date, days_label)
    return exports.export_investigation_csv(start_date, end_date, days_label)
