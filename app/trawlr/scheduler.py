"""
APScheduler configuration for periodic tasks.

Run with: python -m trawlr.scheduler
"""

import os
import sys
import logging

# Set Django settings before importing anything that might use Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'trawlr.settings')
os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', 'true')

import django
django.setup()

from django.db import connections
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger


def close_db_connections():
    """
    Close all database connections to prevent pool exhaustion.

    The scheduler runs as a long-lived process making periodic DB queries.
    Without explicit cleanup, connections stay open indefinitely and can
    exhaust PostgreSQL's connection pool.
    """
    connections.close_all()


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def init_settings():
    """Ensure GlobalSettings exists with default values."""
    from accounts.models import GlobalSettings
    settings, created = GlobalSettings.objects.get_or_create(pk=1)
    if created:
        logger.info('Created GlobalSettings with default values')
    return settings


def get_scheduler_settings():
    """Get scheduler settings from the database."""
    settings = init_settings()
    return {
        'download_queue_interval': settings.download_queue_interval,
        'channel_sync_interval': settings.channel_sync_interval,
        'channel_stats_interval': settings.channel_stats_interval,
        'media_counts_interval': settings.media_counts_interval,
        'stuck_task_recovery_interval': settings.stuck_task_recovery_interval,
        'availability_check_interval': settings.availability_check_interval,
        'forum_topics_sync_interval': settings.forum_topics_sync_interval,
        'member_sync_interval': settings.member_sync_interval,
        'reaction_scan_interval': settings.reaction_scan_interval,
    }


def trigger_process_download_queue():
    """Trigger the download queue processor."""
    from tasks import process_download_queue
    logger.debug('Triggering process_download_queue')
    process_download_queue.send()


def trigger_sync_all_account_channels():
    """Trigger channel sync for all accounts."""
    from tasks import sync_all_account_channels
    logger.debug('Triggering sync_all_account_channels')
    sync_all_account_channels.send()


def trigger_refresh_all_channel_stats():
    """Trigger channel stats refresh."""
    from tasks import refresh_all_channel_stats
    logger.debug('Triggering refresh_all_channel_stats')
    refresh_all_channel_stats.send()


def trigger_refresh_all_media_counts():
    """Trigger media counts refresh for all channels."""
    from tasks import refresh_all_media_counts
    logger.debug('Triggering refresh_all_media_counts')
    refresh_all_media_counts.send()


def trigger_check_all_source_availability():
    """Trigger availability check for all channels."""
    from tasks import check_all_source_availability
    logger.debug('Triggering check_all_source_availability')
    check_all_source_availability.send()


def trigger_sync_all_forum_topics():
    """Trigger forum topics sync for all forum channels."""
    from tasks import sync_all_forum_topics
    logger.debug('Triggering sync_all_forum_topics')
    sync_all_forum_topics.send()


def trigger_sync_all_members():
    """Trigger member list sync for all groups/supergroups."""
    from tasks import scan_all_channel_members_for_user, dispatch_task
    logger.debug('Triggering scan_all_channel_members_for_user (periodic member sync)')
    dispatch_task(
        scan_all_channel_members_for_user,
        task_type='scan_members',
    )


def trigger_scan_all_reactions():
    """Trigger reaction scans for all channels with reactions."""
    from tasks import scan_all_channel_reactions
    logger.debug('Triggering scan_all_channel_reactions (periodic reaction scan)')
    scan_all_channel_reactions.send()


def trigger_cleanup_old_dramatiq_tasks():
    """
    Delete django_dramatiq_task rows older than 7 days.

    AdminMiddleware writes ~3-4 rows per dramatiq message; without retention
    the table grows unbounded. Runs daily.
    """
    from django.core.management import call_command
    logger.debug('Triggering delete_old_tasks (dramatiq admin retention)')
    try:
        # django-dramatiq exposes this as a management command.
        # max_task_age is in seconds. 7 days = 604800.
        call_command('delete_old_tasks', max_task_age=604800)
    except Exception as e:
        logger.error(f'delete_old_tasks failed: {e}')


def run_stuck_task_recovery():
    """Run recovery for stuck tasks (TaskRuns and DownloadTasks)."""
    from tasks import recover_stuck_tasks
    logger.debug('Running stuck task recovery')
    recovered = recover_stuck_tasks()
    if recovered['task_runs'] or recovered['download_tasks']:
        logger.info(f"Recovered {recovered['task_runs']} TaskRuns, {recovered['download_tasks']} DownloadTasks")


def main():
    """Start the scheduler."""
    # Run stuck task recovery on startup
    logger.info('Running startup recovery for stuck tasks...')
    run_stuck_task_recovery()

    # Load settings from database
    settings = get_scheduler_settings()
    download_queue_interval = settings['download_queue_interval']
    channel_sync_interval = settings['channel_sync_interval']
    channel_stats_interval = settings['channel_stats_interval']
    media_counts_interval = settings['media_counts_interval']
    stuck_task_recovery_interval = settings['stuck_task_recovery_interval']
    availability_check_interval = settings['availability_check_interval']
    forum_topics_sync_interval = settings['forum_topics_sync_interval']
    member_sync_interval = settings['member_sync_interval']
    reaction_scan_interval = settings['reaction_scan_interval']

    scheduler = BlockingScheduler()

    # Process download queue at configured interval (0 = disabled)
    if download_queue_interval > 0:
        scheduler.add_job(
            trigger_process_download_queue,
            trigger=IntervalTrigger(seconds=download_queue_interval),
            id='process_download_queue',
            name='Process download queue',
            replace_existing=True,
        )
    else:
        logger.info('Download queue processing is DISABLED')

    # Sync channels for all accounts at configured interval (0 = disabled)
    if channel_sync_interval > 0:
        scheduler.add_job(
            trigger_sync_all_account_channels,
            trigger=IntervalTrigger(seconds=channel_sync_interval),
            id='sync_all_account_channels',
            name='Sync account channels',
            replace_existing=True,
        )
    else:
        logger.info('Channel sync is DISABLED')

    # Refresh channel stats at configured interval (0 = disabled)
    if channel_stats_interval > 0:
        scheduler.add_job(
            trigger_refresh_all_channel_stats,
            trigger=IntervalTrigger(seconds=channel_stats_interval),
            id='refresh_all_channel_stats',
            name='Refresh channel stats',
            replace_existing=True,
        )
    else:
        logger.info('Channel stats refresh is DISABLED')

    # Refresh media counts at configured interval (0 = disabled)
    if media_counts_interval > 0:
        scheduler.add_job(
            trigger_refresh_all_media_counts,
            trigger=IntervalTrigger(seconds=media_counts_interval),
            id='refresh_all_media_counts',
            name='Refresh media counts',
            replace_existing=True,
        )
    else:
        logger.info('Media counts refresh is DISABLED')

    # Run stuck task recovery at configured interval (0 = disabled, runs on startup only)
    if stuck_task_recovery_interval > 0:
        scheduler.add_job(
            run_stuck_task_recovery,
            trigger=IntervalTrigger(seconds=stuck_task_recovery_interval),
            id='stuck_task_recovery',
            name='Stuck task recovery',
            replace_existing=True,
        )
    else:
        logger.info('Periodic stuck task recovery is DISABLED (runs on startup only)')

    # Check source availability at configured interval (0 = disabled)
    if availability_check_interval > 0:
        scheduler.add_job(
            trigger_check_all_source_availability,
            trigger=IntervalTrigger(seconds=availability_check_interval),
            id='check_all_source_availability',
            name='Check source availability',
            replace_existing=True,
        )
    else:
        logger.info('Availability check is DISABLED')

    # Sync forum topics at configured interval (0 = disabled)
    if forum_topics_sync_interval > 0:
        scheduler.add_job(
            trigger_sync_all_forum_topics,
            trigger=IntervalTrigger(seconds=forum_topics_sync_interval),
            id='sync_all_forum_topics',
            name='Sync forum topics',
            replace_existing=True,
        )
    else:
        logger.info('Forum topics sync is DISABLED')

    # Sync group/supergroup member lists at configured interval (0 = disabled)
    if member_sync_interval > 0:
        scheduler.add_job(
            trigger_sync_all_members,
            trigger=IntervalTrigger(seconds=member_sync_interval),
            id='sync_all_members',
            name='Sync group members',
            replace_existing=True,
        )
    else:
        logger.info('Member sync is DISABLED')

    # Scan per-user reactions at configured interval (0 = disabled)
    if reaction_scan_interval > 0:
        scheduler.add_job(
            trigger_scan_all_reactions,
            trigger=IntervalTrigger(seconds=reaction_scan_interval),
            id='scan_all_reactions',
            name='Scan channel reactions',
            replace_existing=True,
        )
    else:
        logger.info('Reaction scanning is DISABLED')

    # Retain django_dramatiq_task rows for 7 days (hardcoded daily interval).
    # Not user-configurable — this is operational housekeeping, not a feature.
    scheduler.add_job(
        trigger_cleanup_old_dramatiq_tasks,
        trigger=IntervalTrigger(seconds=86400),
        id='cleanup_old_dramatiq_tasks',
        name='Cleanup old dramatiq admin tasks (7d retention)',
        replace_existing=True,
    )

    logger.info('Starting Trawlr scheduler...')
    jobs = scheduler.get_jobs()
    if jobs:
        logger.info('Scheduled jobs:')
        for job in jobs:
            logger.info(f'  - {job.name} (every {job.trigger.interval.total_seconds()}s)')
    else:
        logger.info('No scheduled jobs configured')
    logger.info('Note: Changes to scheduler settings require a restart to take effect.')

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info('Scheduler stopped.')
        scheduler.shutdown()


if __name__ == '__main__':
    main()
