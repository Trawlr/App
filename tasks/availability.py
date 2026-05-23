"""
Availability check tasks for Telegram channels.
"""

import uuid

import dramatiq
from django.utils import timezone

from accounts.models import GlobalSettings, TelegramAccount
from accounts.telegram_service import TelegramService, run_async
from audit.models import ActivityLog, TelegramChannel
from downloads.models import TaskRun

from .base import logger, QUEUE_DEFAULT, QUEUE_SCANS_MEMBERS, _log_activity, dispatch_task


def _auto_archive_if_enabled(channel, status, error_msg):
    """
    Auto-archive a source if the setting is enabled and the source is unavailable.
    Only archives sources that are currently active.
    """
    if status == 'active' or not channel.active:
        return

    settings = GlobalSettings.get_settings()
    if not settings.auto_archive_unavailable:
        return

    # Archive the source
    channel.active = False
    channel.save(update_fields=['active'])

    # Disable auto-download
    if hasattr(channel, 'config'):
        channel.config.auto_download_enabled = False
        channel.config.save(update_fields=['auto_download_enabled'])

    # Log the activity
    ActivityLog.log(
        'source_auto_archived',
        f'Auto-archived: {channel.title} ({status}: {error_msg})',
        source='worker',
        channel=channel,
        status=status,
        error=error_msg,
    )

    logger.info(f"Auto-archived source {channel.title} (status: {status})")


@dramatiq.actor(queue_name=QUEUE_SCANS_MEMBERS)
def check_source_availability(channel_id: int, task_run_id: str = None):
    """
    Check availability of a single source.
    Returns the status and error message.
    """
    logger.info(f"check_source_availability: Checking channel {channel_id}")

    # Get TaskRun if provided
    task_run = None
    if task_run_id:
        try:
            task_run = TaskRun.objects.get(task_id=task_run_id)
            # Check if task was cancelled before we even start
            if task_run.should_cancel or task_run.status in ('cancelled', 'completed', 'failed'):
                logger.info(f"TaskRun {task_run_id} already cancelled/completed, skipping")
                return {'status': 'skipped', 'error': 'Task already cancelled/completed'}
            task_run.mark_running()
        except TaskRun.DoesNotExist:
            logger.warning(f"TaskRun {task_run_id} not found")

    try:
        channel = TelegramChannel.objects.select_related('account').get(pk=channel_id)
    except TelegramChannel.DoesNotExist:
        logger.error(f"Channel {channel_id} not found")
        if task_run:
            task_run.mark_failed("Channel not found")
        return {'status': 'error', 'error': 'Channel not found'}

    account = channel.account

    if account.is_flood_wait_active:
        logger.warning(f"Account {account.phone_number} is in flood wait")
        if task_run:
            task_run.mark_failed("Account is in flood wait")
        return {'status': 'error', 'error': 'Account is in flood wait, try again later'}

    try:
        service = TelegramService(account)

        async def check_channel():
            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number
            )

            try:
                # Get dialogs to populate entity cache
                global_settings = GlobalSettings.get_settings()
                await service._client.get_dialogs(limit=global_settings.dialog_cache_limit)

                status = 'active'
                error_msg = ''

                try:
                    # Try to get the entity
                    entity = await service._client.get_entity(channel.telegram_id)

                    # Check if entity is restricted by Telegram (e.g., TOS violation)
                    # Only mark as restricted if platform is "all" - platform-specific
                    # restrictions (ios, android) are still accessible via API
                    is_globally_restricted = False
                    if getattr(entity, 'restricted', False):
                        reasons = getattr(entity, 'restriction_reason', [])
                        for reason in reasons:
                            if getattr(reason, 'platform', '') == 'all':
                                is_globally_restricted = True
                                error_msg = getattr(reason, 'text', 'Channel is restricted by Telegram')
                                break

                    if is_globally_restricted:
                        status = 'restricted'
                    else:
                        # Entity exists and not globally restricted, verify we can access content
                        try:
                            messages = await service._client.get_messages(entity, limit=1)
                            status = 'active'
                        except Exception as access_err:
                            error_str = str(access_err).lower()
                            error_class = type(access_err).__name__.lower()
                            logger.info(f"Channel {channel.title} entity exists but message access failed: {error_class}: {access_err}")

                            if 'channel_private' in error_str or 'channelprivate' in error_class:
                                status = 'private'
                                error_msg = 'Channel is private or access was revoked'
                            elif 'channel_banned' in error_str or 'channelbanned' in error_class:
                                status = 'deleted'
                                error_msg = 'Channel was banned by Telegram'
                            elif 'chat_forbidden' in error_str or 'chatforbidden' in error_class:
                                status = 'private'
                                error_msg = 'Access to this chat is forbidden'
                            else:
                                status = 'unavailable'
                                error_msg = f'Cannot access content: {type(access_err).__name__}: {access_err}'

                except ValueError as e:
                    error_str = str(e).lower()
                    if 'could not find' in error_str or 'no user' in error_str:
                        status = 'deleted'
                        error_msg = 'Channel no longer accessible (may be deleted or banned)'
                    else:
                        status = 'unknown'
                        error_msg = str(e)

                except Exception as e:
                    error_str = str(e).lower()
                    error_class = type(e).__name__.lower()

                    if 'channelprivate' in error_class or 'channel_private' in error_str:
                        status = 'private'
                        error_msg = 'Channel is private or access was revoked'
                    elif 'chatforbidden' in error_class or 'chat_forbidden' in error_str:
                        status = 'private'
                        error_msg = 'Access to this chat is forbidden'
                    elif 'channelinvalid' in error_class or 'channel_invalid' in error_str:
                        status = 'deleted'
                        error_msg = 'Channel no longer exists (deleted or banned by Telegram)'
                    elif 'chatinvalid' in error_class or 'chat_invalid' in error_str:
                        status = 'deleted'
                        error_msg = 'Chat no longer exists'
                    elif 'userbanned' in error_class or 'user_banned' in error_str:
                        status = 'restricted'
                        error_msg = 'You are banned from this channel'
                    elif 'chatwriteforbidden' in error_class:
                        status = 'active'
                    else:
                        status = 'unavailable'
                        error_msg = f'{type(e).__name__}: {e}'

                # Update the channel status
                channel.availability_status = status
                channel.availability_checked_at = timezone.now()
                channel.availability_error = error_msg
                channel.save(update_fields=['availability_status', 'availability_checked_at', 'availability_error'])

                return {'status': status, 'error': error_msg}

            finally:
                await service.disconnect()

        result = run_async(check_channel())
        logger.info(f"Availability check for {channel.title}: {result['status']}")

        # Auto-archive if enabled and source is unavailable
        _auto_archive_if_enabled(channel, result['status'], result.get('error', ''))

        # Mark TaskRun as completed
        if task_run:
            task_run.mark_completed()

        return result

    except Exception as e:
        logger.exception(f"Error checking channel {channel_id}: {e}")
        # Mark TaskRun as failed
        if task_run:
            task_run.mark_failed(str(e)[:500])
        return {'status': 'error', 'error': str(e)}


@dramatiq.actor(queue_name=QUEUE_DEFAULT)
def check_account_availability(account_id: int, channel_ids: list = None, task_run_id: str = None):
    """
    Check availability of channels for a specific account in a single connection.

    This is the efficient batch version that:
    - Uses a single Telegram connection for all channels
    - Tracks progress so it can resume if interrupted
    - Handles flood waits by re-queuing remaining channels
    - Dispatches individual retry tasks for failed channels

    Args:
        account_id: The TelegramAccount ID to check channels for
        channel_ids: Optional list of specific channel IDs to check (for resume).
                     If None, checks all channels for the account.
        task_run_id: TaskRun ID for progress tracking
    """
    from telethon.errors import FloodWaitError

    logger.info(f"check_account_availability: Starting for account {account_id}")

    # Get TaskRun if provided
    task_run = None
    if task_run_id:
        try:
            task_run = TaskRun.objects.get(task_id=task_run_id)
            if task_run.should_cancel or task_run.status in ('cancelled', 'completed', 'failed'):
                logger.info(f"TaskRun {task_run_id} already cancelled/completed, skipping")
                return {'status': 'skipped', 'reason': 'Task already cancelled/completed'}
            task_run.mark_running()
        except TaskRun.DoesNotExist:
            logger.warning(f"TaskRun {task_run_id} not found")

    # Get the account
    try:
        account = TelegramAccount.objects.get(pk=account_id, is_active=True, is_authenticated=True)
    except TelegramAccount.DoesNotExist:
        logger.error(f"Account {account_id} not found or not active")
        if task_run:
            task_run.mark_failed("Account not found or not active")
        return {'status': 'error', 'error': 'Account not found'}

    # Get channels to check
    if channel_ids:
        channels = list(TelegramChannel.objects.filter(pk__in=channel_ids, account=account))
    else:
        channels = list(TelegramChannel.objects.filter(account=account, active=True))

    if not channels:
        logger.info(f"No channels to check for account {account_id}")
        if task_run:
            task_run.mark_completed()
        return {'status': 'completed', 'checked': 0}

    # Initialize progress tracking
    progress = task_run.progress if task_run else {}
    checked_ids = set(progress.get('checked_ids', []))
    failed_ids = progress.get('failed_ids', [])
    total_checked = progress.get('total_checked', 0)
    total_unavailable = progress.get('total_unavailable', 0)

    # Filter out already-checked channels (for resume)
    remaining_channels = [c for c in channels if c.pk not in checked_ids]

    if not remaining_channels:
        logger.info(f"All channels already checked for account {account_id}")
        if task_run:
            task_run.mark_completed()
        return {'status': 'completed', 'checked': total_checked}

    logger.info(f"Checking {len(remaining_channels)} channels for account {account.phone_number}")

    # Update progress message
    if task_run:
        task_run.update_progress(
            message=f"Checking {len(remaining_channels)} channels",
            percent=int((len(checked_ids) / len(channels)) * 100) if channels else 0,
            data={'checked_ids': list(checked_ids), 'failed_ids': failed_ids,
                  'total_checked': total_checked, 'total_unavailable': total_unavailable}
        )

    flood_wait_triggered = False
    flood_wait_seconds = 0

    try:
        service = TelegramService(account)

        async def check_channels_batch():
            nonlocal total_checked, total_unavailable, checked_ids, failed_ids
            nonlocal flood_wait_triggered, flood_wait_seconds

            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number
            )

            try:
                # Get dialogs once to populate entity cache
                global_settings = GlobalSettings.get_settings()
                logger.info(f"Fetching dialogs for account {account.phone_number}")
                try:
                    await service._client.get_dialogs(limit=global_settings.dialog_cache_limit)
                except FloodWaitError as e:
                    flood_wait_triggered = True
                    flood_wait_seconds = e.seconds
                    logger.warning(f"Flood wait {e.seconds}s during dialogs fetch, will re-queue")
                    return

                for channel in remaining_channels:
                    # Check for cancellation
                    if task_run:
                        task_run.refresh_from_db(fields=['should_cancel', 'status'])
                        if task_run.should_cancel or task_run.status == 'cancelled':
                            logger.info(f"Task cancelled, stopping at channel {channel.pk}")
                            break

                    status = 'active'
                    error_msg = ''
                    channel_failed = False

                    try:
                        # Try to get the entity
                        entity = await service._client.get_entity(channel.telegram_id)

                        # Check if entity is restricted
                        is_globally_restricted = False
                        if getattr(entity, 'restricted', False):
                            reasons = getattr(entity, 'restriction_reason', [])
                            for reason in reasons:
                                if getattr(reason, 'platform', '') == 'all':
                                    is_globally_restricted = True
                                    error_msg = getattr(reason, 'text', 'Channel is restricted by Telegram')
                                    break

                        if is_globally_restricted:
                            status = 'restricted'
                        else:
                            # Verify we can access content
                            try:
                                await service._client.get_messages(entity, limit=1)
                                status = 'active'
                            except FloodWaitError as e:
                                flood_wait_triggered = True
                                flood_wait_seconds = e.seconds
                                logger.warning(f"Flood wait {e.seconds}s at channel {channel.pk}, will re-queue remaining")
                                return
                            except Exception as access_err:
                                error_str = str(access_err).lower()
                                error_class = type(access_err).__name__.lower()

                                if 'channel_private' in error_str or 'channelprivate' in error_class:
                                    status = 'private'
                                    error_msg = 'Channel is private or access was revoked'
                                elif 'channel_banned' in error_str or 'channelbanned' in error_class:
                                    status = 'deleted'
                                    error_msg = 'Channel was banned by Telegram'
                                elif 'chat_forbidden' in error_str or 'chatforbidden' in error_class:
                                    status = 'private'
                                    error_msg = 'Access to this chat is forbidden'
                                else:
                                    status = 'unavailable'
                                    error_msg = f'Cannot access content: {type(access_err).__name__}'

                    except FloodWaitError as e:
                        flood_wait_triggered = True
                        flood_wait_seconds = e.seconds
                        logger.warning(f"Flood wait {e.seconds}s at channel {channel.pk}, will re-queue remaining")
                        return

                    except ValueError as e:
                        error_str = str(e).lower()
                        if 'could not find' in error_str or 'no user' in error_str:
                            status = 'deleted'
                            error_msg = 'Channel no longer accessible (may be deleted or banned)'
                        else:
                            status = 'unknown'
                            error_msg = str(e)
                            channel_failed = True

                    except Exception as e:
                        error_str = str(e).lower()
                        error_class = type(e).__name__.lower()

                        if 'channelprivate' in error_class or 'channel_private' in error_str:
                            status = 'private'
                            error_msg = 'Channel is private or access was revoked'
                        elif 'chatforbidden' in error_class or 'chat_forbidden' in error_str:
                            status = 'private'
                            error_msg = 'Access to this chat is forbidden'
                        elif 'channelinvalid' in error_class or 'channel_invalid' in error_str:
                            status = 'deleted'
                            error_msg = 'Channel no longer exists'
                        elif 'chatinvalid' in error_class or 'chat_invalid' in error_str:
                            status = 'deleted'
                            error_msg = 'Chat no longer exists'
                        elif 'userbanned' in error_class or 'user_banned' in error_str:
                            status = 'restricted'
                            error_msg = 'You are banned from this channel'
                        elif 'chatwriteforbidden' in error_class:
                            status = 'active'
                        else:
                            status = 'unavailable'
                            error_msg = f'{type(e).__name__}: {e}'
                            channel_failed = True

                    # Update the channel status
                    total_checked += 1
                    checked_ids.add(channel.pk)

                    if status != 'active':
                        total_unavailable += 1
                        logger.info(f"Channel {channel.title}: {status}")

                    if channel_failed:
                        failed_ids.append(channel.pk)

                    channel.availability_status = status
                    channel.availability_checked_at = timezone.now()
                    channel.availability_error = error_msg
                    channel.save(update_fields=['availability_status', 'availability_checked_at', 'availability_error'])

                    # Auto-archive if enabled and source is unavailable
                    _auto_archive_if_enabled(channel, status, error_msg)

                    # Update progress periodically
                    if task_run and total_checked % 10 == 0:
                        task_run.update_progress(
                            message=f"Checked {total_checked}/{len(channels)} channels",
                            percent=int((len(checked_ids) / len(channels)) * 100),
                            data={'checked_ids': list(checked_ids), 'failed_ids': failed_ids,
                                  'total_checked': total_checked, 'total_unavailable': total_unavailable}
                        )

            finally:
                await service.disconnect()

        run_async(check_channels_batch())

        # Handle flood wait - re-queue remaining channels with delay
        if flood_wait_triggered:
            remaining_ids = [c.pk for c in remaining_channels if c.pk not in checked_ids]

            if remaining_ids:
                logger.info(f"Re-queuing {len(remaining_ids)} channels after {flood_wait_seconds}s flood wait")

                # Save progress before re-queuing
                if task_run:
                    task_run.update_progress(
                        message=f"Flood wait - resuming {len(remaining_ids)} channels in {flood_wait_seconds}s",
                        data={'checked_ids': list(checked_ids), 'failed_ids': failed_ids,
                              'total_checked': total_checked, 'total_unavailable': total_unavailable}
                    )

                # Re-dispatch with remaining channels after flood wait delay
                delay_ms = (flood_wait_seconds + 5) * 1000  # Add 5s buffer
                check_account_availability.send_with_options(
                    args=(account_id,),
                    kwargs={'channel_ids': remaining_ids, 'task_run_id': task_run_id},
                    delay=delay_ms
                )

                return {
                    'status': 'flood_wait',
                    'checked': total_checked,
                    'remaining': len(remaining_ids),
                    'retry_in_seconds': flood_wait_seconds + 5
                }

        # Dispatch individual retry tasks for failed channels
        if failed_ids:
            logger.info(f"Dispatching {len(failed_ids)} individual retry tasks for failed channels")
            for channel_id in failed_ids:
                try:
                    check_source_availability.send_with_options(
                        args=(channel_id,),
                        delay=60000  # 1 minute delay for retry
                    )
                except Exception as e:
                    logger.error(f"Failed to dispatch retry for channel {channel_id}: {e}")

        # Mark task as completed
        if task_run:
            task_run.update_progress(
                message=f"Completed: {total_checked} checked, {total_unavailable} unavailable",
                percent=100,
                data={'checked_ids': list(checked_ids), 'failed_ids': failed_ids,
                      'total_checked': total_checked, 'total_unavailable': total_unavailable}
            )
            task_run.mark_completed()

        logger.info(f"Availability check complete for account {account_id}: {total_checked} checked, {total_unavailable} unavailable")

        return {
            'status': 'completed',
            'checked': total_checked,
            'unavailable': total_unavailable,
            'failed_retries': len(failed_ids)
        }

    except Exception as e:
        logger.exception(f"Error checking channels for account {account_id}: {e}")
        if task_run:
            task_run.update_progress(
                data={'checked_ids': list(checked_ids), 'failed_ids': failed_ids,
                      'total_checked': total_checked, 'total_unavailable': total_unavailable}
            )
            task_run.mark_failed(str(e)[:500])
        raise


@dramatiq.actor(queue_name=QUEUE_DEFAULT)
def check_all_source_availability():
    """
    Dispatcher that queues per-account batch availability check tasks.

    Instead of creating one task per channel (inefficient - 448 tasks for 448 channels),
    this creates one task per account (efficient - ~2-3 tasks for all channels).

    Each batch task:
    - Uses a single Telegram connection for all channels in that account
    - Handles flood waits by saving progress and re-queuing remaining channels
    - Dispatches individual retry tasks for any channels that fail
    """

    logger.info("check_all_source_availability: Dispatching per-account batch tasks")

    # Create TaskRun record for tracking (global task, no channel/account)
    task_id = str(uuid.uuid4())
    task_run = TaskRun.create_task('availability_check_all', task_id)
    task_run.mark_running()

    # Get all active/authenticated accounts that have channels
    accounts = TelegramAccount.objects.filter(
        is_active=True,
        is_authenticated=True,
        channels__active=True,
    ).distinct()

    dispatched = 0
    skipped = 0
    failed = 0
    total_channels = 0

    for account in accounts:
        # Skip if account is in flood wait
        if account.is_flood_wait_active:
            channel_count = account.channels.filter(active=True).count()
            logger.info(f"Skipping account {account.phone_number} ({channel_count} channels) - flood wait active")
            skipped += channel_count
            continue

        channel_count = account.channels.filter(active=True).count()
        total_channels += channel_count

        # Dispatch one batch task per account
        try:
            dispatch_task(
                check_account_availability,
                'check_availability',
                account=account,
                args=(account.pk,),
            )
            dispatched += 1
            logger.info(f"Dispatched batch task for account {account.phone_number} ({channel_count} channels)")
        except Exception as e:
            failed += 1
            logger.error(f"Failed to dispatch batch task for account {account.phone_number}: {e}")

    logger.info(
        f"check_all_source_availability: Dispatched {dispatched} batch tasks "
        f"covering {total_channels} channels, skipped {skipped}, failed {failed}"
    )

    # Mark task as completed with progress data
    task_run.update_progress(data={
        'dispatched_batches': dispatched,
        'total_channels': total_channels,
        'skipped': skipped,
        'failed': failed
    })
    task_run.mark_completed()

    return {
        'success': True,
        'dispatched_batches': dispatched,
        'total_channels': total_channels,
        'skipped': skipped,
        'failed': failed
    }
