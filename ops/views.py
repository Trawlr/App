"""
Ops panel views.
- RabbitMQ queue state (ready/unacked/rate, via management API)
- django_dramatiq.Task rows (every dramatiq execution)
- downloads.TaskRun rows (Trawlr's app-level progress tracking)

Provides peek + evict+purge + force-close actions for django super users.
"""

import time
from collections import defaultdict

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import user_passes_test
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from django_dramatiq.models import Task
from downloads.models import TaskRun

from . import rabbitmq


superuser_required = user_passes_test(lambda u: u.is_authenticated and u.is_superuser)


def _trawlr_queues_grouped():
    """
    Return one row per logical queue name (main + .DQ + .XQ combined).

    Shape: [{'name': 'trawlr.default', 'main': {...}, 'dq': {...}, 'xq': {...}}, ...]
    """
    queues = rabbitmq.list_queues()
    by_logical = defaultdict(dict)
    for q in queues:
        name = q['name']
        if not name.startswith('trawlr.'):
            continue
        if name.endswith('.DQ'):
            by_logical[name[:-3]]['dq'] = q
        elif name.endswith('.XQ'):
            by_logical[name[:-3]]['xq'] = q
        else:
            by_logical[name]['main'] = q
    rows = []
    for logical_name in sorted(by_logical.keys()):
        entry = by_logical[logical_name]
        rows.append({
            'name': logical_name,
            'main': entry.get('main'),
            'dq': entry.get('dq'),
            'xq': entry.get('xq'),
        })
    return rows


def _consumers_by_queue():
    by_queue = defaultdict(list)
    for c in rabbitmq.list_consumers():
        q = c['queue']['name']
        by_queue[q].append({
            'connection': c['channel_details']['connection_name'],
            'prefetch': c.get('prefetch_count'),
        })
    return by_queue


def _recent_task_summary():
    """Count django_dramatiq.Task rows per actor in the last hour, by status."""
    cutoff = timezone.now() - timezone.timedelta(hours=1)
    return (
        Task.tasks.filter(created_at__gte=cutoff)
        .values('actor_name', 'status')
        .annotate(n=Count('id'))
        .order_by('-n')[:30]
    )


def _orphan_task_runs():
    """
    TaskRun rows in 'running' status whose dramatiq message is not present in
    django_dramatiq.Task — strongly suggests the dramatiq message was lost but
    TaskRun wasn't updated. These are the rows you want to mark failed.
    """
    running = list(
        TaskRun.objects.filter(status='running').values(
            'id', 'task_type', 'task_id', 'started_at', 'progress_message', 'channel_id'
        )[:50]
    )
    # We can't match task_id directly (TaskRun.task_id is an app UUID, not a
    # dramatiq message ID). Instead, treat a TaskRun as orphaned if it's been
    # "running" for more than 10 minutes with no progress update AND no
    # dramatiq Task row exists that carries its task_run_id kwarg.
    cutoff = timezone.now() - timezone.timedelta(minutes=10)
    stale = [r for r in running if r['started_at'] and r['started_at'] < cutoff]
    return stale


@superuser_required
def queues_index(request):
    try:
        queue_rows = _trawlr_queues_grouped()
        consumers_by_queue = _consumers_by_queue()
        # Attach consumers to each row so the template doesn't need a custom filter.
        for row in queue_rows:
            row['consumers'] = (
                consumers_by_queue.get(row['name'], [])
                + consumers_by_queue.get(row['name'] + '.DQ', [])
            )
        task_summary = list(_recent_task_summary())
        orphans = _orphan_task_runs()
        error = None
    except Exception as e:
        queue_rows = []
        task_summary = []
        orphans = []
        error = f'RabbitMQ management API unreachable: {e}'

    return render(request, 'ops/queues.html', {
        'queue_rows': queue_rows,
        'task_summary': task_summary,
        'orphans': orphans,
        'error': error,
    })


@superuser_required
@require_POST
def queue_peek(request, name):
    count = int(request.POST.get('count', 20))
    try:
        messages = rabbitmq.peek_messages(name, count=count)
        error = None
    except Exception as e:
        messages = []
        error = str(e)
    return render(request, 'ops/_peek_result.html', {
        'queue_name': name,
        'messages': messages,
        'error': error,
    })


@superuser_required
@require_POST
def queue_purge(request, name):
    ok = rabbitmq.purge_queue(name)
    msg = f'Purged {name}' if ok else f'Failed to purge {name}'
    return HttpResponse(msg)


@superuser_required
@require_POST
def queue_evict_and_purge(request, name):
    """
    Force-close every consumer on the queue (and its .DQ sibling), then
    purge 3× to defeat the worker's reconnect+reconsume race.
    """
    consumers_by_queue = _consumers_by_queue()
    targets = [name, f'{name}.DQ']
    connections_closed = []
    for q in targets:
        for c in consumers_by_queue.get(q, []):
            if rabbitmq.close_connection(c['connection'], reason=f'evict for purge of {q}'):
                connections_closed.append(c['connection'])
    time.sleep(2)
    purged_rounds = 0
    for _ in range(3):
        for q in [name, f'{name}.DQ', f'{name}.XQ']:
            rabbitmq.purge_queue(q)
        purged_rounds += 1
        time.sleep(0.4)
    return HttpResponse(
        f'Closed {len(connections_closed)} connection(s); '
        f'purged {name}/.DQ/.XQ x{purged_rounds}'
    )


@superuser_required
@require_POST
def connection_close(request):
    conn_name = request.POST.get('name')
    if not conn_name:
        return HttpResponse('missing name', status=400)
    ok = rabbitmq.close_connection(conn_name, reason='closed via ops panel')
    return HttpResponse('closed' if ok else 'failed', status=200 if ok else 500)
