import uuid

import dramatiq
from django.utils import timezone
from telethon.errors import ChannelInvalidError, ChannelPrivateError, FloodWaitError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest

from accounts.models import GlobalSettings
from accounts.telegram_service import TelegramService, run_async
from audit.models import TelegramChannel
from downloads.models import TaskRun

from .base import logger, QUEUE_DEFAULT, QUEUE_SCANS_MEMBERS, _log_activity, dispatch_task
from .sync import sync_forum_topics


@dramatiq.actor(queue_name=QUEUE_DEFAULT, max_retries=2, min_backoff=30000)
def fetch_media_counts(channel_id: int, task_run_id: str = None):
    """
    Fetch and cache Telegram media counts for a channel.
    Uses GetSearchCountersRequest to get counts of photos, videos, and files.
    """
    logger.info(f"fetch_media_counts: Starting for channel {channel_id}")

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
        channel = TelegramChannel.objects.select_related('account').get(pk=channel_id)
    except TelegramChannel.DoesNotExist:
        logger.error(f"Channel {channel_id} not found")
        if task_run:
            task_run.mark_failed("Channel not found")
        return {'success': False, 'error': 'Channel not found'}

    # Create TaskRun if not provided
    if not task_run:
        if TaskRun.is_task_running('fetch_media_counts', channel=channel):
            logger.info(f"Media counts fetch already running for {channel.title}")
            return {'success': False, 'error': 'Already running'}
        task_run = TaskRun.create_task('fetch_media_counts', str(uuid.uuid4()), channel=channel)

    task_run.mark_running()

    account = channel.account

    if not account.is_authenticated:
        logger.warning(f"Account for channel {channel_id} is not authenticated")
        task_run.mark_failed("Account not authenticated")
        return {'success': False, 'error': 'Account not authenticated'}

    if account.is_flood_wait_active:
        logger.warning(f"Account is in flood wait, rescheduling fetch_media_counts")
        task_run.update_progress("Waiting for flood wait to clear")
        fetch_media_counts.send_with_options(
            args=(channel_id,),
            kwargs={'task_run_id': task_run.task_id},
            delay=60000
        )
        return {'success': False, 'error': 'Account in flood wait, rescheduled'}

    logger.info(f"Fetching media counts for channel {channel.title} (ID: {channel_id})")

    try:
        service = TelegramService(account)

        async def get_counts():
            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number
            )
            try:
                return await service.get_media_counts(channel.telegram_id, channel.username)
            finally:
                await service.disconnect()

        result = run_async(get_counts())

        if result['success']:
            channel.telegram_photo_count = result['counts']['photos']
            channel.telegram_video_count = result['counts']['videos']
            channel.telegram_file_count = result['counts']['files']
            channel.telegram_counts_updated_at = timezone.now()
            channel.save(update_fields=[
                'telegram_photo_count', 'telegram_video_count',
                'telegram_file_count', 'telegram_counts_updated_at'
            ])

            logger.info(f"Updated media counts for {channel.title}: "
                       f"photos={result['counts']['photos']}, "
                       f"videos={result['counts']['videos']}, "
                       f"files={result['counts']['files']}")

            _log_activity(
                'media_counts_updated',
                f"Updated media counts for {channel.title}",
                channel=channel,
                photo_count=result['counts']['photos'],
                video_count=result['counts']['videos'],
                file_count=result['counts']['files'],
            )

            task_run.update_progress(data=result['counts'])
            task_run.mark_completed()
            return {'success': True, 'counts': result['counts']}
        else:
            logger.warning(f"Failed to get media counts for {channel.title}: {result.get('error')}")
            task_run.mark_failed(result.get('error', 'Unknown error')[:500])
            return result

    except Exception as e:
        logger.exception(f"Error fetching media counts for channel {channel_id}")
        task_run.mark_failed(str(e)[:500])
        return {'success': False, 'error': str(e)}


@dramatiq.actor(queue_name=QUEUE_DEFAULT)
def refresh_all_media_counts():
    """
    Dispatcher that queues fetch_media_counts tasks for all active channels.
    Uses staggered delays (1 second apart) to avoid rate limiting.
    Runs on QUEUE_DEFAULT for parallel I/O via gevent.
    """
    logger.info("refresh_all_media_counts: Dispatching media count refresh tasks")

    # Create TaskRun record for tracking (global task, no channel/account)
    task_id = str(uuid.uuid4())
    task_run = TaskRun.create_task('media_counts', task_id)
    task_run.mark_running()

    # Get all active channels with active/authenticated accounts
    channels = TelegramChannel.objects.filter(
        active=True,
        availability_status='active',
        account__is_active=True,
        account__is_authenticated=True,
    ).select_related('account')

    dispatched = 0
    skipped = 0
    failed = 0

    for channel in channels:
        # Skip if account is in flood wait
        if channel.account.is_flood_wait_active:
            logger.debug(f"Skipping {channel.title} (account in flood wait)")
            skipped += 1
            continue

        # Skip if already running
        if TaskRun.is_task_running('fetch_media_counts', channel=channel):
            logger.debug(f"Skipping {channel.title} (task already running)")
            skipped += 1
            continue

        # Atomically create TaskRun and dispatch with retry logic
        try:
            dispatch_task(
                fetch_media_counts,
                'fetch_media_counts',
                channel=channel,
                args=(channel.pk,),
            )
            dispatched += 1
        except Exception as e:
            failed += 1
            logger.error(f"Failed to dispatch fetch_media_counts for {channel.title}: {e}")

    logger.info(f"refresh_all_media_counts: Dispatched {dispatched} tasks, skipped {skipped}, failed {failed}")

    # Mark task as completed with progress data
    task_run.update_progress(data={'dispatched': dispatched, 'skipped': skipped, 'failed': failed})
    task_run.mark_completed()

    return {'success': True, 'dispatched': dispatched, 'skipped': skipped, 'failed': failed}


@dramatiq.actor(queue_name=QUEUE_DEFAULT)
def refresh_all_channel_stats():
    """
    Dispatcher that queues individual refresh_channel_stats tasks for all active channels.
    Each channel gets its own task for parallel processing via worker_telegram.
    """
    logger.info("refresh_all_channel_stats: Dispatching stats refresh tasks")

    # Get all active channels with active/authenticated accounts
    channels = TelegramChannel.objects.filter(
        active=True,
        availability_status='active',
        account__is_active=True,
        account__is_authenticated=True,
    ).select_related('account')

    # Pre-fetch all channels with running refresh_stats tasks in ONE query
    # This avoids N queries for N channels
    running_channel_ids = set(
        TaskRun.objects.filter(
            task_type='refresh_stats',
            status__in=['queued', 'running'],
            channel__isnull=False
        ).values_list('channel_id', flat=True)
    )

    dispatched = 0
    skipped = 0
    failed = 0

    for channel in channels:
        # Skip if account is in flood wait
        if channel.account.is_flood_wait_active:
            logger.debug(f"Skipping {channel.title} (account in flood wait)")
            skipped += 1
            continue

        # Skip if a refresh_stats task is already running for this channel (checked via pre-fetched set)
        if channel.pk in running_channel_ids:
            logger.debug(f"Skipping {channel.title} (task already running)")
            skipped += 1
            continue

        # Atomically create TaskRun and dispatch with retry logic
        try:
            dispatch_task(
                refresh_channel_stats,
                'refresh_stats',
                channel=channel,
                account=channel.account,
                args=(channel.pk,),
            )
            dispatched += 1
        except Exception as e:
            failed += 1
            logger.error(f"Failed to dispatch refresh_stats for {channel.title}: {e}")

    logger.info(f"refresh_all_channel_stats: Dispatched {dispatched} tasks, skipped {skipped}, failed {failed}")
    return {'success': True, 'dispatched': dispatched, 'skipped': skipped, 'failed': failed}


@dramatiq.actor(queue_name=QUEUE_SCANS_MEMBERS, max_retries=2, min_backoff=30000)
def refresh_channel_stats(channel_id: int, task_run_id: str = None):
    """
    Refresh statistics for a single channel.
    Updates member count from Telegram API.
    Runs on QUEUE_SCANS_MEMBERS for rate limit protection.
    """
    logger.info(f"refresh_channel_stats: Starting for channel {channel_id}")

    # Get TaskRun if provided
    task_run = None
    if task_run_id:
        try:
            task_run = TaskRun.objects.get(task_id=task_run_id)
            # Check if task was cancelled before we even start
            if task_run.should_cancel or task_run.status in ('cancelled', 'completed', 'failed'):
                logger.info(f"TaskRun {task_run_id} already cancelled/completed, skipping")
                return {'success': False, 'error': 'Task already cancelled/completed'}
            task_run.mark_running()
        except TaskRun.DoesNotExist:
            logger.warning(f"TaskRun {task_run_id} not found")

    try:
        channel = TelegramChannel.objects.select_related('account').get(pk=channel_id)
    except TelegramChannel.DoesNotExist:
        logger.error(f"Channel {channel_id} not found")
        if task_run:
            task_run.mark_failed("Channel not found")
        return {'success': False, 'error': 'Channel not found'}

    # Skip inactive sources
    if not channel.active:
        logger.info(f"Channel {channel.title} is inactive, skipping stats refresh")
        if task_run:
            task_run.mark_completed()
        return {'success': False, 'error': 'Channel inactive'}

    account = channel.account

    if not account.is_authenticated or not account.is_active:
        logger.warning(f"Account for channel {channel_id} is not active/authenticated")
        if task_run:
            task_run.mark_failed("Account not active")
        return {'success': False, 'error': 'Account not active'}

    # Skip if account is in flood wait the daily scheduler will pick it up next cycle
    if account.is_flood_wait_active:
        logger.info(f"Account in flood wait, skipping stats refresh for {channel.title}")
        if task_run:
            task_run.mark_completed()
        return {'success': False, 'error': 'Account in flood wait, skipped'}

    # Log refresh stats started (only once we're actually going to do work)
    _log_activity(
        'refresh_stats_started',
        f"Starting stats refresh for {channel.title}",
        source='worker_telegram',
        channel=channel,
    )

    try:
        service = TelegramService(account)

        async def get_stats():
            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number
            )

            try:
                # Populate entity cache
                await service._client.get_dialogs(limit=GlobalSettings.get_settings().dialog_cache_limit)

                participants_count = 0
                is_forum = None  # None means don't update

                if channel.channel_type in ('channel', 'supergroup'):
                    # Use GetFullChannelRequest for channels/supergroups
                    full_info = await service._client(
                        GetFullChannelRequest(channel.telegram_id)
                    )
                    participants_count = getattr(full_info.full_chat, 'participants_count', 0)
                    # Extract forum flag from the channel entity in chats
                    if full_info.chats:
                        chat_entity = full_info.chats[0]
                        is_forum = getattr(chat_entity, 'forum', False) or False

                elif channel.channel_type == 'group':
                    # Use GetFullChatRequest for regular groups
                    full_info = await service._client(
                        GetFullChatRequest(channel.telegram_id)
                    )
                    participants_count = getattr(full_info.full_chat, 'participants_count', 0)

                elif channel.channel_type == 'private':
                    # Private chats always have 2 participants (you and the other person)
                    participants_count = 2

                return {
                    'success': True,
                    'participants_count': participants_count,
                    'is_forum': is_forum,
                }

            finally:
                await service.disconnect()

        result = run_async(get_stats())

        if result['success']:
            new_member_count = result['participants_count']
            old_member_count = channel.member_count
            new_is_forum = result.get('is_forum')

            update_fields = []
            if new_member_count != old_member_count:
                channel.member_count = new_member_count
                update_fields.append('member_count')
                logger.info(f"Updated {channel.title}: member_count {old_member_count} -> {new_member_count}")

            if new_is_forum is not None and new_is_forum != channel.is_forum:
                old_is_forum = channel.is_forum
                channel.is_forum = new_is_forum
                update_fields.append('is_forum')
                logger.info(f"Updated {channel.title}: is_forum -> {new_is_forum}")
                # When channel becomes a forum, pre-sync topics
                if new_is_forum and not old_is_forum:
                    sync_forum_topics.send(channel.pk)
                    logger.info(f"Queued topic sync for newly detected forum {channel.title}")

            if update_fields:
                update_fields.append('updated_at')
                channel.save(update_fields=update_fields)
            else:
                logger.debug(f"No change for {channel.title}: member_count={new_member_count}")

            if task_run:
                task_run.mark_completed()

            # Log refresh stats completed
            _log_activity(
                'refresh_stats_completed',
                f"Stats refresh complete for {channel.title}: {new_member_count} members",
                source='worker_telegram',
                channel=channel,
                member_count=new_member_count,
            )

            return {'success': True, 'member_count': new_member_count}

    except FloodWaitError as e:
        logger.warning(f"Flood wait for {channel.title}: {e.seconds}s")
        account.flood_wait_until = timezone.now() + timezone.timedelta(seconds=e.seconds)
        account.save(update_fields=['flood_wait_until'])
        if task_run:
            task_run.mark_failed(f"Flood wait: {e.seconds}s")
        return {'success': False, 'error': f'Flood wait: {e.seconds}s'}

    except (ChannelPrivateError, ChannelInvalidError) as e:
        logger.warning(f"Cannot access channel {channel.title}: {e}")
        if task_run:
            task_run.mark_failed(str(e))
        return {'success': False, 'error': str(e)}

    except Exception as e:
        logger.exception(f"Error refreshing stats for channel {channel_id}")
        if task_run:
            task_run.mark_failed(str(e)[:500])
        return {'success': False, 'error': str(e)}
