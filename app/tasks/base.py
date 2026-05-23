"""
Shared imports, constants, and helpers for Dramatiq tasks.
"""

import hashlib
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

# Must be set before any Django DB operations when using gevent
os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', 'true')

import trawlr.dramatiq_config  # noqa: F401

import dramatiq
from dramatiq import Retry
from django.conf import settings
from django.utils import timezone

from accounts.models import GlobalSettings
from audit.models import ActivityLog
from downloads.consumers import sync_broadcast_progress, sync_broadcast_status_change
from downloads.models import TaskRun
from listeners.handlers import _extract_entities_data, _async_create_message_entities

logger = logging.getLogger('trawlr.tasks')

# Queue names from config
QUEUE_DEFAULT = 'trawlr.default'
QUEUE_DOWNLOADS = 'trawlr.downloads'
QUEUE_SCANS_HISTORY = 'trawlr.scans.history'
QUEUE_SCANS_MEMBERS = 'trawlr.scans.members'

# Timeout for stuck tasks (4 hours - matches Dramatiq TimeLimit)
STUCK_TASK_TIMEOUT_HOURS = 4


def _log_activity(activity_type, description, source='worker', channel=None, **details):
    """
    Log an activity to the ActivityLog table.
    This is the sync version for use in Dramatiq tasks.
    """
    try:
        ActivityLog.log(
            activity_type=activity_type,
            description=description,
            source=source,
            channel=channel,
            **details
        )
    except Exception as e:
        logger.debug(f"Failed to log activity: {e}")


def dispatch_task(
    actor,
    task_type: str,
    channel=None,
    account=None,
    args: tuple = (),
    kwargs: dict = None,
    delay: int = None,
    task_id: str = None,
):
    """
    Atomically create a TaskRun and dispatch a Dramatiq message with retry logic.

    This ensures that if message dispatch fails the TaskRun is marked as failed
    rather than being left orphaned in 'queued' status.

    Args:
        actor: The Dramatiq actor to send the message to
        task_type: The task type string for TaskRun (e.g., 'scan_history')
        channel: Optional TelegramChannel for the TaskRun
        account: Optional TelegramAccount for the TaskRun
        args: Positional arguments to pass to the actor
        kwargs: Keyword arguments to pass to the actor (task_run_id will be added)
        delay: Optional delay in milliseconds before the message is processed
        task_id: Optional task_id to use (generates UUID if not provided)

    Returns:
        tuple: (task_run, task_id) on success

    Raises:
        Exception: If all retry attempts fail, raises the last exception
    """
    if kwargs is None:
        kwargs = {}

    # Generate task_id if not provided
    if task_id is None:
        task_id = str(uuid.uuid4())

    # Get retry settings from GlobalSettings
    global_settings = GlobalSettings.get_settings()
    max_retries = global_settings.default_retry_count

    # Create the TaskRun first
    task_run = TaskRun.create_task(task_type, task_id, channel=channel, account=account)

    # Add task_run_id to kwargs for the actor
    kwargs['task_run_id'] = task_id

    # Attempt to dispatch with retries
    last_exception = None
    for attempt in range(max_retries + 1):
        try:
            if delay is not None:
                actor.send_with_options(args=args, kwargs=kwargs, delay=delay)
            else:
                actor.send(*args, **kwargs)

            logger.debug(f"dispatch_task: Successfully dispatched {task_type} (task_id={task_id[:8]})")
            return task_run, task_id

        except Exception as e:
            last_exception = e
            if attempt < max_retries:
                # Exponential backoff: 10s, 20s, 40s
                backoff = 10 * (2 ** attempt)
                logger.warning(
                    f"dispatch_task: Failed to dispatch {task_type} (attempt {attempt + 1}/{max_retries + 1}), "
                    f"retrying in {backoff:.1f}s: {e}"
                )
                time.sleep(backoff)
            else:
                logger.error(
                    f"dispatch_task: All {max_retries + 1} attempts failed for {task_type} "
                    f"(task_id={task_id[:8]}): {e}"
                )

    # All retries exhausted - mark TaskRun as failed
    task_run.mark_failed(f"Failed to dispatch message after {max_retries + 1} attempts: {last_exception}")
    raise last_exception
