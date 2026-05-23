"""
Channel and forum topic sync tasks.
"""

import uuid

import dramatiq
from django.utils import timezone
from telethon.tl.functions.messages import GetForumTopicsRequest
from telethon.tl.types import Channel, Chat, ForumTopic as TelethonForumTopic, User

from accounts.models import GlobalSettings, TelegramAccount
from accounts.telegram_service import TelegramService, run_async
from audit.models import ForumTopic, TelegramChannel
from downloads.models import TaskRun

from .base import logger, QUEUE_DEFAULT, QUEUE_SCANS_HISTORY, _log_activity, dispatch_task
from .onboarding import run_channel_onboarding


@dramatiq.actor(queue_name=QUEUE_SCANS_HISTORY)
def sync_account_channels(account_id: int, task_run_id: str = None):
    """
    Sync sources from a Telegram account.
    This fetches all dialogs and updates the database.
    """
    logger.info(f"sync_account_channels: Starting sync for account {account_id}")

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
        account = TelegramAccount.objects.get(pk=account_id)
    except TelegramAccount.DoesNotExist:
        logger.error(f"Account {account_id} not found")
        if task_run:
            task_run.mark_failed("Account not found")
        return {'success': False, 'error': 'Account not found'}

    if not account.is_authenticated:
        logger.warning(f"Account {account_id} is not authenticated")
        if task_run:
            task_run.mark_failed("Account not authenticated")
        return {'success': False, 'error': 'Account not authenticated'}

    if account.is_flood_wait_active:
        logger.warning(f"Account {account_id} is in flood wait, rescheduling")
        if task_run:
            task_run.update_progress("Waiting for flood wait to clear")
        sync_account_channels.send_with_options(args=(account_id,), kwargs={'task_run_id': task_run_id}, delay=60000)
        return {'success': False, 'error': 'Account in flood wait, rescheduled'}

    # Log sync started
    _log_activity(
        'channel_sync_started',
        f"Starting channel sync for {account.name}",
        source='worker_telegram',
        account_id=account_id,
    )

    if task_run:
        task_run.update_progress("Connecting to Telegram...")

    try:
        service = TelegramService(account)

        async def sync_dialogs():
            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number
            )
            try:
                global_settings = GlobalSettings.get_settings()
                dialogs = await service.get_dialogs(limit=global_settings.dialog_cache_limit)
                return dialogs
            finally:
                await service.disconnect()

        dialogs = run_async(sync_dialogs())

        if task_run:
            task_run.update_progress(f"Processing {len(dialogs)} dialogs...", percent=10)

        # Process dialogs
        created_count = 0
        updated_count = 0
        total_dialogs = len(dialogs)

        for i, dialog in enumerate(dialogs):
            # Check for cancellation
            if task_run:
                task_run.refresh_from_db()
                if task_run.should_cancel:
                    task_run.mark_cancelled()
                    _log_activity(
                        'channel_sync_cancelled',
                        f"Channel sync cancelled for {account.name}",
                        source='worker_telegram',
                        account_id=account_id,
                    )
                    return {'success': False, 'cancelled': True}

            entity = dialog.entity

            # Determine channel type
            if isinstance(entity, Channel):
                if entity.megagroup:
                    channel_type = 'supergroup'
                else:
                    channel_type = 'channel'
                is_private = not entity.username
            elif isinstance(entity, Chat):
                channel_type = 'group'
                is_private = True
            elif isinstance(entity, User):
                channel_type = 'private'
                is_private = True
            else:
                continue

            # Create or update channel
            channel, created = TelegramChannel.objects.update_or_create(
                telegram_id=entity.id,
                defaults={
                    'account': account,
                    'title': dialog.name or 'Unknown',
                    'username': getattr(entity, 'username', None),
                    'channel_type': channel_type,
                    'is_private': is_private,
                    'member_count': getattr(entity, 'participants_count', 0) or 0,
                    # entity.date is the join date, dialog.date is last message date
                    'joined_at': getattr(entity, 'date', None),
                    # Forum/topics support (only for Channel entities)
                    'is_forum': getattr(entity, 'forum', False) or False,
                }
            )

            if created:
                created_count += 1
                # Check if onboarding should run for newly detected channels
                settings = GlobalSettings.get_settings()
                if settings.run_onboarding_for_new_sources and not channel.onboarded:
                    channel.onboarded = True
                    channel.save(update_fields=['onboarded'])
                    # Atomically create TaskRun and dispatch with retry logic
                    try:
                        dispatch_task(
                            run_channel_onboarding,
                            'onboarding',
                            channel=channel,
                            args=(channel.pk,),
                        )
                        logger.info(f"Queued onboarding for new channel: {channel.title}")
                    except Exception as e:
                        logger.error(f"Failed to queue onboarding for {channel.title}: {e}")
            else:
                updated_count += 1

            # Update progress every 10 dialogs
            if task_run and (i + 1) % 10 == 0:
                percent = 10 + int((i + 1) / total_dialogs * 90)
                task_run.update_progress(f"Processed {i + 1}/{total_dialogs} dialogs", percent=percent)

        channel_count = TelegramChannel.objects.filter(account=account).count()

        logger.info(f"sync_account_channels: Synced {created_count} new, {updated_count} updated for account {account_id}")

        # Mark task as completed
        if task_run:
            task_run.progress = {
                'created': created_count,
                'updated': updated_count,
                'total_channels': channel_count,
            }
            task_run.mark_completed()

        _log_activity(
            'channel_sync_completed',
            f"Synced channels for {account.name}: {created_count} new, {updated_count} updated",
            source='worker_telegram',
            account_id=account_id,
            created_count=created_count,
            updated_count=updated_count,
            total_channels=channel_count,
        )

        return {
            'success': True,
            'created': created_count,
            'updated': updated_count,
            'channel_count': channel_count,
        }

    except Exception as e:
        logger.exception(f"Error syncing channels for account {account_id}")
        if task_run:
            task_run.mark_failed(str(e)[:500])
        _log_activity(
            'error',
            f"Failed to sync channels for {account.name}: {str(e)[:200]}",
            source='worker_telegram',
            account_id=account_id,
            error=str(e),
        )
        return {'success': False, 'error': str(e)}


@dramatiq.actor(queue_name=QUEUE_DEFAULT)
def sync_all_account_channels():
    """
    Sync channels for all authenticated accounts.
    Scheduled task that triggers sync_account_channels for each active account.
    Creates TaskRun entries for tracking in the UI.
    """
    logger.info("sync_all_account_channels: Starting sync for all accounts")

    accounts = TelegramAccount.objects.filter(
        is_active=True,
        is_authenticated=True
    )

    # Pre-fetch all accounts with running sync_channels tasks in ONE query
    # This avoids N queries for N accounts
    running_account_ids = set(
        TaskRun.objects.filter(
            task_type='sync_channels',
            status__in=['queued', 'running'],
            account__isnull=False
        ).values_list('account_id', flat=True)
    )

    dispatched = 0
    skipped_flood = 0
    skipped_running = 0

    for account in accounts:
        # Skip if account is in flood wait
        if account.is_flood_wait_active:
            logger.info(f"Skipping account {account.name} (in flood wait)")
            skipped_flood += 1
            continue

        # Skip if a sync_channels task is already running for this account (checked via pre-fetched set)
        if account.pk in running_account_ids:
            logger.info(f"Skipping account {account.name} (sync already running)")
            skipped_running += 1
            continue

        # Atomically create TaskRun and dispatch with retry logic
        try:
            dispatch_task(
                sync_account_channels,
                'sync_channels',
                account=account,
                args=(account.pk,),
            )
            dispatched += 1
            logger.info(f"Triggered channel sync for account: {account.name}")
        except Exception as e:
            logger.error(f"Failed to dispatch sync for account {account.name}: {e}")

    logger.info(f"sync_all_account_channels: Dispatched {dispatched}, skipped {skipped_flood} (flood wait), {skipped_running} (already running)")
    return {'success': True, 'dispatched': dispatched, 'skipped_flood': skipped_flood, 'skipped_running': skipped_running}


@dramatiq.actor(queue_name=QUEUE_DEFAULT, max_retries=2, min_backoff=30000, max_backoff=120000)
def sync_forum_topics(channel_id: int, task_run_id: str = None):
    """
    Sync forum topics for a channel from Telegram.

    Fetches all topics for a forum-enabled supergroup and updates the ForumTopic
    records with actual titles and metadata.

    Args:
        channel_id: TelegramChannel primary key
        task_run_id: Optional TaskRun ID for tracking
    """
    logger.info(f"sync_forum_topics: Starting for channel {channel_id}")

    try:
        channel = TelegramChannel.objects.select_related('account').get(pk=channel_id)
    except TelegramChannel.DoesNotExist:
        logger.error(f"Channel {channel_id} not found")
        return {'success': False, 'error': 'Channel not found'}

    # Skip if source is inactive (archived)
    if not channel.active:
        logger.debug(f"Channel {channel.title} is inactive, skipping topic sync")
        return {'success': False, 'error': 'Source is inactive'}

    # Skip if source is deleted, banned, restricted, or unavailable
    if channel.availability_status in ('deleted', 'restricted', 'private', 'unavailable'):
        logger.debug(f"Channel {channel.title} is {channel.availability_status}, skipping topic sync")
        return {'success': False, 'error': f'Source is {channel.availability_status}'}

    # Skip if account has left this source
    if channel.has_left:
        logger.debug(f"Channel {channel.title} has been left, skipping topic sync")
        return {'success': False, 'error': 'Account has left this source'}

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

    # Create TaskRun if not provided (for backwards compatibility with existing queued tasks)
    # Use atomic get_or_create to prevent race conditions when multiple tasks start simultaneously
    if not task_run:
        task_run, created = TaskRun.get_or_create_if_not_running('sync_topics', channel=channel)
        if not created:
            logger.info(f"Topic sync already running for {channel.title}")
            return {'success': False, 'error': 'Already running'}

    task_run.mark_running()

    # Skip if not a forum
    if not channel.is_forum:
        logger.debug(f"Channel {channel.title} is not a forum, skipping topic sync")
        task_run.mark_completed()
        return {'success': False, 'error': 'Not a forum'}

    account = channel.account

    # Skip if account not active
    if not account.is_authenticated or not account.is_active:
        logger.warning(f"Account {account.pk} not active/authenticated")
        task_run.mark_failed('Account not active')
        return {'success': False, 'error': 'Account not active'}

    # Check flood wait - reschedule instead of failing
    if account.is_flood_wait_active:
        logger.warning(f"Account is in flood wait, rescheduling topic sync")
        task_run.update_progress("Waiting for flood wait to clear")
        sync_forum_topics.send_with_options(
            args=(channel_id,),
            kwargs={'task_run_id': task_run.task_id},
            delay=60000
        )
        return {'success': False, 'error': 'Account in flood wait, rescheduled'}

    try:
        service = TelegramService(account)

        async def do_sync():
            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number
            )

            try:
                # Get entity for the channel
                try:
                    entity = await service._client.get_entity(channel.telegram_id)
                except ValueError:
                    logger.info(f"Entity {channel.telegram_id} not cached, fetching dialogs...")
                    await service._client.get_dialogs(limit=GlobalSettings.get_settings().dialog_cache_limit)
                    entity = await service._client.get_entity(channel.telegram_id)

                # Fetch forum topics using GetForumTopicsRequest

                result = await service._client(GetForumTopicsRequest(
                    peer=entity,
                    offset_date=None,
                    offset_id=0,
                    offset_topic=0,
                    limit=100,  # Max topics per request
                ))

                topics_updated = 0
                topics_created = 0

                for topic in result.topics:
                    if not isinstance(topic, TelethonForumTopic):
                        continue

                    topic_data = {
                        'title': topic.title,
                        'icon_color': getattr(topic, 'icon_color', None),
                        'icon_emoji_id': getattr(topic, 'icon_emoji_id', None),
                        'is_closed': getattr(topic, 'closed', False) or False,
                        'is_hidden': getattr(topic, 'hidden', False) or False,
                        'is_pinned': getattr(topic, 'pinned', False) or False,
                        'is_general': topic.id == 1,
                    }

                    forum_topic, created = ForumTopic.objects.update_or_create(
                        channel=channel,
                        topic_id=topic.id,
                        defaults=topic_data
                    )

                    if created:
                        topics_created += 1
                    else:
                        topics_updated += 1

                logger.info(f"Topic sync completed for {channel.title}: {topics_created} created, {topics_updated} updated")
                return {'success': True, 'created': topics_created, 'updated': topics_updated}

            finally:
                await service.disconnect()

        result = run_async(do_sync())

        # Mark task completed
        if task_run:
            task_run.update_progress(
                message=f"Synced {result.get('created', 0)} new, {result.get('updated', 0)} updated",
                percent=100,
                data=result
            )
            task_run.mark_completed()

        return result

    except Exception as e:
        logger.exception(f"Error syncing forum topics for channel {channel_id}")
        if task_run:
            task_run.mark_failed(str(e)[:500])
        return {'success': False, 'error': str(e)}


@dramatiq.actor(queue_name=QUEUE_DEFAULT)
def sync_all_forum_topics():
    """
    Sync forum topics for all forum-enabled channels.
    Scheduled task that triggers sync_forum_topics for each active forum channel.
    """
    logger.info("sync_all_forum_topics: Starting sync for all forum channels")

    # Get all active forum channels from active accounts
    forum_channels = TelegramChannel.objects.filter(
        account__is_active=True,
        account__is_authenticated=True,
        is_forum=True,
        active=True,
        availability_status='active',
        has_left=False,
    ).select_related('account')

    # Pre-fetch channels with running sync_topics tasks
    already_running_channel_ids = set(
        TaskRun.objects.filter(
            task_type='sync_topics',
            status__in=['queued', 'running'],
        ).values_list('channel_id', flat=True)
    )

    # Pre-fetch accounts in flood wait
    flood_wait_account_ids = set(
        TelegramAccount.objects.filter(
            is_active=True,
            flood_wait_until__gt=timezone.now()
        ).values_list('pk', flat=True)
    )

    dispatched = 0
    skipped_running = 0
    skipped_flood = 0

    for channel in forum_channels:
        # Skip if account is in flood wait
        if channel.account_id in flood_wait_account_ids:
            logger.debug(f"Skipping topic sync for {channel.title} (account in flood wait)")
            skipped_flood += 1
            continue

        # Skip if already running
        if channel.pk in already_running_channel_ids:
            logger.debug(f"Skipping topic sync for {channel.title} (already running)")
            skipped_running += 1
            continue

        # Dispatch the task
        try:
            dispatch_task(
                sync_forum_topics,
                'sync_topics',
                channel=channel,
                args=(channel.pk,),
            )
            dispatched += 1
            logger.debug(f"Queued topic sync for {channel.title}")
        except Exception as e:
            logger.error(f"Failed to dispatch topic sync for {channel.title}: {e}")

    logger.info(f"sync_all_forum_topics: Dispatched {dispatched}, skipped {skipped_running} (already running), {skipped_flood} (flood wait)")
    return {'success': True, 'dispatched': dispatched, 'skipped_running': skipped_running, 'skipped_flood': skipped_flood}
