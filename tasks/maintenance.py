"""
Maintenance tasks for cleanup and recovery.
"""

import uuid
from datetime import timedelta

import dramatiq
import requests
from django.conf import settings as django_settings
from django.utils import timezone

from downloads.models import DownloadTask, TaskRun

from .base import (
    logger,
    QUEUE_DEFAULT,
    STUCK_TASK_TIMEOUT_HOURS,
)

# XQ queues to check for dead letters
XQ_QUEUES = [
    'trawlr.default.XQ',
    'trawlr.downloads.XQ',
    'trawlr.scans.history.XQ',
    'trawlr.scans.members.XQ',
    'trawlr.events.telegram.XQ',
]


def recover_stuck_tasks():
    """
    Recover tasks that are stuck in running/downloading state.

    This should be called:
    1. On worker/scheduler startup
    2. Periodically (e.g., every hour) by the scheduler

    Returns dict with counts of recovered items.
    """
    # Create TaskRun record for tracking (global task, no channel/account)
    task_id = str(uuid.uuid4())
    tracking_task = TaskRun.create_task('stuck_recovery', task_id)
    tracking_task.mark_running()

    cutoff_time = timezone.now() - timedelta(hours=STUCK_TASK_TIMEOUT_HOURS)
    recovered = {'task_runs': 0, 'download_tasks': 0}

    # Recover stuck TaskRuns (running/queued for too long)
    # Use bulk update for efficiency - single query instead of N queries
    stuck_task_runs = TaskRun.objects.filter(
        status__in=['running', 'queued'],
        created_at__lt=cutoff_time
    ).exclude(pk=tracking_task.pk)  # Don't recover our own tracking task

    stuck_task_run_ids = list(stuck_task_runs.values_list('pk', 'task_type', 'status', 'created_at'))
    for pk, task_type, status, created_at in stuck_task_run_ids:
        logger.warning(
            f"Recovering stuck TaskRun: id={pk}, type={task_type}, "
            f"status={status}, created_at={created_at}"
        )

    if stuck_task_run_ids:
        # Bulk update all stuck TaskRuns in a single query
        recovered['task_runs'] = TaskRun.objects.filter(
            pk__in=[pk for pk, _, _, _ in stuck_task_run_ids]
        ).update(
            status='failed',
            completed_at=timezone.now(),
            error=f"Task stuck for over {STUCK_TASK_TIMEOUT_HOURS} hours - recovered on startup"
        )

    # Recover stuck DownloadTasks (downloading for too long)
    # Use select_related to avoid N+1 for logging, then bulk update
    stuck_downloads = DownloadTask.objects.filter(
        status='downloading',
        started_at__lt=cutoff_time
    ).select_related('channel')

    stuck_download_ids = []
    for task in stuck_downloads:
        logger.warning(
            f"Recovering stuck DownloadTask: id={task.pk}, channel={task.channel.title}, "
            f"started_at={task.started_at}"
        )
        stuck_download_ids.append(task.pk)

    if stuck_download_ids:
        # Bulk update all stuck DownloadTasks in a single query
        recovered['download_tasks'] = DownloadTask.objects.filter(
            pk__in=stuck_download_ids
        ).update(
            status='pending',
            pending_reason='queued',
            progress=0,
            downloaded_bytes=0,
            celery_task_id=''
        )

    if recovered['task_runs'] or recovered['download_tasks']:
        logger.info(
            f"Recovery complete: {recovered['task_runs']} TaskRuns failed, "
            f"{recovered['download_tasks']} DownloadTasks reset to pending"
        )

    # Mark tracking task as completed with progress data
    tracking_task.update_progress(data=recovered)
    tracking_task.mark_completed()

    return recovered


@dramatiq.actor(queue_name=QUEUE_DEFAULT)
def cleanup_completed_tasks(days_old: int = 7):
    """Clean up old completed download tasks."""
    cutoff = timezone.now() - timedelta(days=days_old)
    deleted, _ = DownloadTask.objects.filter(
        status='completed',
        completed_at__lt=cutoff
    ).delete()
    logger.info(f"Cleaned up {deleted} old completed tasks")


def _get_rabbitmq_api_url():
    """Get RabbitMQ management API URL and credentials from settings."""
    host = django_settings.RABBITMQ_HOST
    user = django_settings.RABBITMQ_USER
    password = django_settings.RABBITMQ_PASSWORD
    return f"http://{host}:15672", user, password


def get_dead_letter_stats():
    """
    Get counts of messages in all XQ (dead letter) queues.

    Returns dict with queue names and message counts.
    """
    base_url, user, password = _get_rabbitmq_api_url()
    if not base_url:
        logger.error("Could not parse RabbitMQ URL")
        return {'error': 'Could not parse RabbitMQ URL'}

    stats = {}
    total = 0

    try:
        for queue_name in XQ_QUEUES:
            encoded_queue = queue_name.replace('.', '%2E')
            url = f"{base_url}/api/queues/%2F/{encoded_queue}"
            response = requests.get(url, auth=(user, password), timeout=10)

            if response.status_code == 200:
                data = response.json()
                count = data.get('messages', 0)
                if count > 0:
                    stats[queue_name] = count
                    total += count
            elif response.status_code == 404:
                # Queue doesn't exist, skip
                pass
            else:
                logger.warning(f"Failed to get stats for {queue_name}: {response.status_code}")
    except Exception as e:
        logger.exception(f"Error getting dead letter stats: {e}")
        return {'error': str(e)}

    stats['total'] = total
    return stats


def requeue_dead_letters():
    """
    Requeue all messages from XQ (dead letter) queues back to their original queues.

    This moves failed messages back to be retried. Use with caution as messages
    may have failed for a reason (e.g., bad data, missing resources).

    Returns dict with counts of requeued messages per queue.
    """
    base_url, user, password = _get_rabbitmq_api_url()
    if not base_url:
        logger.error("Could not parse RabbitMQ URL")
        return {'success': False, 'error': 'Could not parse RabbitMQ URL'}

    # Create TaskRun for tracking
    task_id = str(uuid.uuid4())
    task_run = TaskRun.create_task('requeue_dead_letters', task_id)
    task_run.mark_running()

    results = {}
    total_requeued = 0

    try:
        for xq_name in XQ_QUEUES:
            # Original queue name is XQ name without .XQ suffix
            original_queue = xq_name[:-3]  # Remove .XQ

            requeued = 0
            encoded_xq = xq_name.replace('.', '%2E')

            # Get messages from XQ and requeue them
            while True:
                # Get one message at a time
                url = f"{base_url}/api/queues/%2F/{encoded_xq}/get"
                payload = {
                    'count': 1,
                    'ackmode': 'ack_requeue_false',  # Remove from XQ
                    'encoding': 'auto'
                }

                response = requests.post(url, json=payload, auth=(user, password), timeout=10)

                if response.status_code != 200:
                    break

                messages = response.json()
                if not messages:
                    break

                msg = messages[0]
                payload_str = msg.get('payload', '{}')
                properties = msg.get('properties', {})

                # Publish to original queue
                encoded_original = original_queue.replace('.', '%2E')
                publish_url = f"{base_url}/api/exchanges/%2F//publish"
                publish_payload = {
                    'routing_key': original_queue,
                    'payload': payload_str,
                    'payload_encoding': 'string',
                    'properties': {
                        'delivery_mode': 2,  # Persistent
                        'headers': {}  # Clear x-death headers
                    }
                }

                pub_response = requests.post(publish_url, json=publish_payload, auth=(user, password), timeout=10)

                if pub_response.status_code == 200 and pub_response.json().get('routed'):
                    requeued += 1
                    logger.info(f"Requeued message from {xq_name} to {original_queue}")
                else:
                    logger.warning(f"Failed to requeue message to {original_queue}: {pub_response.text}")

            if requeued > 0:
                results[xq_name] = requeued
                total_requeued += requeued
                logger.info(f"Requeued {requeued} messages from {xq_name}")

        task_run.update_progress(data={'requeued': results, 'total': total_requeued})
        task_run.mark_completed()

        return {
            'success': True,
            'requeued': results,
            'total': total_requeued
        }

    except Exception as e:
        logger.exception(f"Error requeuing dead letters: {e}")
        task_run.mark_failed(str(e))
        return {'success': False, 'error': str(e)}


def purge_dead_letters():
    """
    Purge (delete) all messages from XQ (dead letter) queues.

    Use this when you want to discard failed messages without retrying.

    Returns dict with counts of purged messages per queue.
    """
    base_url, user, password = _get_rabbitmq_api_url()
    if not base_url:
        logger.error("Could not parse RabbitMQ URL")
        return {'success': False, 'error': 'Could not parse RabbitMQ URL'}

    # Create TaskRun for tracking
    task_id = str(uuid.uuid4())
    task_run = TaskRun.create_task('purge_dead_letters', task_id)
    task_run.mark_running()

    results = {}
    total_purged = 0

    try:
        for xq_name in XQ_QUEUES:
            encoded_xq = xq_name.replace('.', '%2E')

            # Get message count first
            stats_url = f"{base_url}/api/queues/%2F/{encoded_xq}"
            stats_response = requests.get(stats_url, auth=(user, password), timeout=10)

            if stats_response.status_code == 200:
                count = stats_response.json().get('messages', 0)
                if count > 0:
                    # Purge the queue
                    purge_url = f"{base_url}/api/queues/%2F/{encoded_xq}/contents"
                    purge_response = requests.delete(purge_url, auth=(user, password), timeout=10)

                    if purge_response.status_code == 204:
                        results[xq_name] = count
                        total_purged += count
                        logger.info(f"Purged {count} messages from {xq_name}")
                    else:
                        logger.warning(f"Failed to purge {xq_name}: {purge_response.status_code}")

        task_run.update_progress(data={'purged': results, 'total': total_purged})
        task_run.mark_completed()

        return {
            'success': True,
            'purged': results,
            'total': total_purged
        }

    except Exception as e:
        logger.exception(f"Error purging dead letters: {e}")
        task_run.mark_failed(str(e))
        return {'success': False, 'error': str(e)}
