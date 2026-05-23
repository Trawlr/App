"""
Coordinated Inauthentic Behavior (CIB) detector — JSON API.

Single endpoint, tab-discriminated, returns JSON for the CIB page. Mirrors
the activity-heatmap pattern (reports/views_activity.py).
"""

import json

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse, HttpResponseBadRequest, Http404
from django.views.decorators.http import require_http_methods
from silk.profiling.profiler import silk_profile

from audit.models import CIBFlag
from . import queries_cib as qc


@login_required
@silk_profile(name='reports.api_cib')
def api_cib(request):
    """
    JSON for the CIB page. Accepted params (see parse_cib_params):
        tab:           'clusters' | 'chains' | 'crossposters'
        kind:          'text' | 'media' | 'entity'    (clusters/chains tabs)
        days:          1..30                           (default 7)
        min_channels:  2..50                           (default 3)
        window_seconds: 1..86400                       (default 60)
        active_only:   1 | 0                           (default 1)
    """
    params = qc.parse_cib_params(request)
    tab = params['tab']

    if tab in (qc.TAB_CLUSTERS, qc.TAB_CHAINS):
        rows = qc.get_clusters(params, kind=params['kind'])
        flag_kind = params['kind']
    elif tab == qc.TAB_CROSSPOSTERS:
        rows = qc.get_crossposters(params)
        flag_kind = 'crossposter'
    else:
        rows = []
        flag_kind = None

    # Decorate rows with any analyst flags (Phase D). Cheap — single query
    # over the fingerprints we just computed.
    if rows and flag_kind:
        _attach_flags(rows, flag_kind, mixed=(tab == qc.TAB_CROSSPOSTERS))

    return JsonResponse({
        'tab': tab,
        'kind': params['kind'],
        'params': {
            'days': params['days'],
            'min_channels': params['min_channels'],
            'window_seconds': params['window_seconds'],
            'active_only': params['active_only'],
        },
        'rows': rows,
        'summary': {
            'cluster_count': len(rows),
            'truncated': len(rows) >= qc.ROW_LIMIT,
        },
    })


def _attach_flags(rows, single_kind, mixed=False):
    """
    Look up CIBFlag rows by (cluster_kind, fingerprint) and attach to each row.
    `mixed=True` means rows may carry different cluster_kinds (the crossposter
    tab uses 'crossposter' uniformly so it's still single-kind in practice).
    """
    fingerprints = [r['fingerprint'] for r in rows if r.get('fingerprint')]
    if not fingerprints:
        return
    flags = CIBFlag.objects.filter(
        cluster_kind=single_kind,
        cluster_fingerprint__in=fingerprints,
    ).values('id', 'cluster_kind', 'cluster_fingerprint', 'status', 'note',
             'flagged_by__username', 'updated_at')
    by_fp = {f['cluster_fingerprint']: f for f in flags}
    for r in rows:
        flag = by_fp.get(r.get('fingerprint'))
        if not flag:
            r['flag'] = None
            continue
        r['flag'] = {
            'id': flag['id'],
            'status': flag['status'],
            'note': flag['note'],
            'flagged_by': flag['flagged_by__username'],
            'updated_at': flag['updated_at'].isoformat() if flag['updated_at'] else None,
        }


@login_required
@require_http_methods(['POST', 'DELETE'])
def api_cib_flag(request, flag_id=None):
    """
    POST  /reports/api/cib/flag/        — upsert flag {kind, fingerprint, status, note}
    DELETE /reports/api/cib/flag/<id>/  — remove a flag
    """
    if request.method == 'DELETE':
        if flag_id is None:
            return HttpResponseBadRequest('flag_id required')
        deleted, _ = CIBFlag.objects.filter(pk=flag_id).delete()
        if not deleted:
            raise Http404('flag not found')
        return JsonResponse({'ok': True, 'deleted': flag_id})

    # POST
    try:
        payload = json.loads(request.body or b'{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('invalid JSON')

    kind = payload.get('kind')
    fingerprint = payload.get('fingerprint')
    status = payload.get('status')
    note = (payload.get('note') or '').strip()

    valid_kinds = {k for k, _ in CIBFlag.KIND_CHOICES}
    valid_statuses = {s for s, _ in CIBFlag.STATUS_CHOICES}
    if kind not in valid_kinds:
        return HttpResponseBadRequest(f'kind must be one of {sorted(valid_kinds)}')
    if not fingerprint:
        return HttpResponseBadRequest('fingerprint required')
    if status not in valid_statuses:
        return HttpResponseBadRequest(f'status must be one of {sorted(valid_statuses)}')

    with transaction.atomic():
        flag, _created = CIBFlag.objects.update_or_create(
            cluster_kind=kind,
            cluster_fingerprint=fingerprint,
            defaults={
                'status': status,
                'note': note,
                'flagged_by': request.user,
            },
        )

    return JsonResponse({
        'ok': True,
        'flag': {
            'id': flag.id,
            'cluster_kind': flag.cluster_kind,
            'cluster_fingerprint': flag.cluster_fingerprint,
            'status': flag.status,
            'note': flag.note,
            'flagged_by': flag.flagged_by.username if flag.flagged_by else None,
            'updated_at': flag.updated_at.isoformat(),
        },
    })
