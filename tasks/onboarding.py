"""
Onboarding tasks for newly detected channels.
"""

import uuid

import dramatiq

from audit.models import TelegramChannel
from downloads.models import TaskRun

from .base import (
    logger,
    QUEUE_DEFAULT,
    _log_activity,
    dispatch_task,
)
from .scanning import scan_channel_history, scan_channel_members


@dramatiq.actor(queue_name=QUEUE_DEFAULT)
def run_channel_onboarding(channel_id: int, task_run_id: str = None):
    """
    Run onboarding tasks for a newly detected channel.

    This triggers a sequence of tasks to populate initial data for the channel:
    1. scan_channel_history (skip_thumbnails=True) - Fetch message history without thumbnails
    2. scan_channel_members (skip_profile_photos=True) - Fetch member list without profile photos
    3. sync_forum_topics - Sync forum topics (only for forum channels)

    Tasks run independently and continue even if one fails.
    """
    logger.info(f"run_channel_onboarding: Starting for channel {channel_id}")

    # Get or create TaskRun for tracking
    task_run = None
    if task_run_id:
        try:
            task_run = TaskRun.objects.get(task_id=task_run_id)
            if task_run.should_cancel or task_run.status in ('cancelled', 'completed', 'failed'):
                logger.info(f"TaskRun {task_run_id} already cancelled/completed, skipping")
                return {'success': False, 'error': 'Task cancelled'}
        except TaskRun.DoesNotExist:
            pass

    try:
        channel = TelegramChannel.objects.get(pk=channel_id)
    except TelegramChannel.DoesNotExist:
        logger.error(f"run_channel_onboarding: Channel {channel_id} not found")
        if task_run:
            task_run.mark_failed("Channel not found")
        return {'success': False, 'error': 'Channel not found'}

    # Create TaskRun if not provided
    if not task_run:
        if TaskRun.is_task_running('onboarding', channel=channel):
            logger.info(f"Onboarding already running for {channel.title}")
            return {'success': False, 'error': 'Already running'}
        task_run = TaskRun.create_task('onboarding', str(uuid.uuid4()), channel=channel)

    task_run.mark_running()

    # Skip inactive sources
    if not channel.active:
        logger.info(f"run_channel_onboarding: Channel {channel.title} is inactive, skipping")
        task_run.mark_completed()
        return {'success': False, 'error': 'Channel is inactive'}

    logger.info(f"run_channel_onboarding: Starting onboarding for {channel.title} (ID: {channel_id})")

    _log_activity(
        'onboarding_started',
        f"Starting onboarding for {channel.title}",
        source='worker_telegram',
        channel=channel,
    )

    tasks_started = []
    tasks_failed = []

    # 1. Start history scan (skip thumbnails)
    if not TaskRun.is_task_running('scan_history', channel=channel):
        try:
            dispatch_task(
                scan_channel_history,
                'scan_history',
                channel=channel,
                args=(channel_id,),
                kwargs={'skip_thumbnails': True},
            )
            tasks_started.append('scan_history')
            logger.info(f"run_channel_onboarding: Started history scan for {channel.title}")
        except Exception as e:
            tasks_failed.append('scan_history')
            logger.exception(f"run_channel_onboarding: Failed to start history scan for {channel.title}: {e}")
    else:
        logger.info(f"run_channel_onboarding: History scan already running for {channel.title}")

    # 2. Start member scan (skip profile photos)
    if not TaskRun.is_task_running('scan_members', channel=channel):
        try:
            dispatch_task(
                scan_channel_members,
                'scan_members',
                channel=channel,
                args=(channel_id,),
                kwargs={'skip_profile_photos': True},
            )
            tasks_started.append('scan_members')
            logger.info(f"run_channel_onboarding: Started member scan for {channel.title}")
        except Exception as e:
            tasks_failed.append('scan_members')
            logger.exception(f"run_channel_onboarding: Failed to start member scan for {channel.title}: {e}")
    else:
        logger.info(f"run_channel_onboarding: Member scan already running for {channel.title}")

    # 3. Sync forum topics (only for forum channels)
    if channel.is_forum:
        # Import here to avoid circular import
        from .sync import sync_forum_topics
        if not TaskRun.is_task_running('sync_topics', channel=channel):
            try:
                dispatch_task(
                    sync_forum_topics,
                    'sync_topics',
                    channel=channel,
                    args=(channel_id,),
                )
                tasks_started.append('sync_topics')
                logger.info(f"run_channel_onboarding: Started forum topics sync for {channel.title}")
            except Exception as e:
                tasks_failed.append('sync_topics')
                logger.exception(f"run_channel_onboarding: Failed to start forum topics sync for {channel.title}: {e}")
        else:
            logger.info(f"run_channel_onboarding: Forum topics sync already running for {channel.title}")

    _log_activity(
        'onboarding_tasks_queued',
        f"Onboarding tasks queued for {channel.title}: {', '.join(tasks_started) if tasks_started else 'none (already running)'}",
        source='worker_telegram',
        channel=channel,
        tasks_started=tasks_started,
        tasks_failed=tasks_failed,
    )

    # Mark onboarding task as completed (or failed if all subtasks failed)
    task_run.update_progress(
        message=f"Started {len(tasks_started)} tasks" + (f", {len(tasks_failed)} failed" if tasks_failed else ""),
        data={'tasks_started': tasks_started, 'tasks_failed': tasks_failed}
    )
    if tasks_started or not tasks_failed:
        task_run.mark_completed()
    else:
        task_run.mark_failed("All subtasks failed to dispatch")

    return {
        'success': True,
        'channel_id': channel_id,
        'channel_title': channel.title,
        'tasks_started': tasks_started,
    }
