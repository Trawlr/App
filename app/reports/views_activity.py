"""
Activity Map API endpoint.

Single endpoint, scope-discriminated, returns JSON for the heatmap. Used by all
three pages (source/user/aggregate) so the frontend partial can be shared.
"""

from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.urls import reverse
from silk.profiling.profiler import silk_profile

from . import queries_activity as qa


@login_required
@silk_profile(name='reports.api_activity')
def api_activity(request):
    """JSON for the activity heatmap. See queries_activity.parse_activity_params for accepted params."""
    params = qa.parse_activity_params(request)

    # Always compute calendar buckets — the footer summary uses them even on hourdow layout.
    calendar_buckets = qa.get_calendar_buckets(params, params['start'], params['end'])

    if params['layout'] == qa.LAYOUT_HOURDOW:
        primary_buckets = qa.get_hour_dow_buckets(params, params['start'], params['end'])
        anomalies = qa.compute_hourdow_anomalies(primary_buckets)
    else:
        primary_buckets = calendar_buckets
        anomalies = qa.compute_calendar_anomalies(primary_buckets)

    response = {
        'scope': params['scope'],
        'scope_id': params['scope_id'],
        'layout': params['layout'],
        'metric': params['metric'],
        'tz': params['tz_name'],
        'days': params['days'],
        'days_label': params['days_label'],
        'start': params['start'].isoformat(),
        'end': params['end'].isoformat(),
        'buckets': primary_buckets,
        'anomalies': anomalies,
        'summary': qa.compute_summary(params, calendar_buckets),
        'drilldown': _drilldown_template(params),
    }

    if params['compare']:
        compare_start = params['start'] - timedelta(days=params['days'])
        compare_end = params['start']
        if params['layout'] == qa.LAYOUT_HOURDOW:
            response['compare_buckets'] = qa.get_hour_dow_buckets(params, compare_start, compare_end)
        else:
            compare_buckets = qa.get_calendar_buckets(params, compare_start, compare_end)
            # Shift prior-period dates forward by `days` so the i-th day of the prior
            # period maps to the i-th day of the current period — frontend looks up
            # baseline by current-period date, so misaligned keys silently return 0.
            offset = timedelta(days=params['days'])
            for bucket in compare_buckets:
                bucket['date'] = (date.fromisoformat(bucket['date']) + offset).isoformat()
            response['compare_buckets'] = compare_buckets

    return JsonResponse(response)


def _drilldown_template(params):
    """
    URL template for cell click-throughs. None if the metric/scope has no
    sensible drill-down target (e.g. deletes/joins, aggregate scope).
    """
    if not qa.metric_supports_drilldown(params['metric']):
        return None
    extra = '&media=media' if params['metric'] == qa.METRIC_MEDIA else ''
    if params['scope'] == qa.SCOPE_SOURCE and params['scope_id']:
        return reverse('audit:source_posts_v3', args=[params['scope_id']]) + '?start={start}&end={end}' + extra
    if params['scope'] == qa.SCOPE_USER and params['scope_id']:
        return reverse('audit:user_detail', args=[params['scope_id']]) + '?start={start}&end={end}' + extra
    return None
