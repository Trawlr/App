import asyncio

import dramatiq
from asgiref.sync import sync_to_async
from django.utils import timezone
from telethon.errors import FloodWaitError

from accounts.telegram_service import TelegramService, run_async
from audit.models import TelegramChannel, TelegramUser, UserGroupMembership
from downloads.models import TaskRun

from .base import logger, QUEUE_SCANS_MEMBERS, _log_activity


@dramatiq.actor(queue_name=QUEUE_SCANS_MEMBERS, max_retries=2, min_backoff=30000)
def fetch_user_profiles(channel_id: int, task_run_id: str = None):
    """
    Fetch full user profiles (bio, business info, etc.) for all members of a channel.
    Uses GetFullUserRequest for each user with staggered delays to avoid rate limiting.
    """
    logger.info(f"fetch_user_profiles: Starting for channel {channel_id}")

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
        logger.info(f"Channel {channel.title} is inactive, skipping user profiles fetch")
        if task_run:
            task_run.mark_completed()
        return {'success': False, 'error': 'Channel inactive'}

    account = channel.account

    if not account.is_authenticated or not account.is_active:
        logger.warning(f"Account for channel {channel_id} is not active/authenticated")
        if task_run:
            task_run.mark_failed("Account not active")
        return {'success': False, 'error': 'Account not active'}

    if account.is_flood_wait_active:
        logger.warning(f"Account is in flood wait, rescheduling")
        if task_run:
            task_run.update_progress("Waiting for flood wait to clear")
        fetch_user_profiles.send_with_options(
            args=(channel_id,),
            kwargs={'task_run_id': task_run_id},
            delay=60000
        )
        return {'success': False, 'error': 'Account in flood wait, rescheduled'}

    # Get all users in this channel
    # Filter for valid access_hash (not null AND not 0, as 0 is invalid for API calls)
    memberships = list(
        UserGroupMembership.objects.filter(channel=channel)
        .select_related('user')
        .values_list('user_id', flat=True)
    )
    users = list(TelegramUser.objects.filter(
        pk__in=memberships,
        access_hash__isnull=False
    ).exclude(access_hash=0))

    total_users = len(users)
    if total_users == 0:
        logger.info(f"No users with access_hash found for channel {channel.title}")
        if task_run:
            task_run.mark_completed()
        return {'success': True, 'fetched': 0, 'skipped': 0}

    logger.info(f"fetch_user_profiles: Found {total_users} users to fetch for {channel.title}")

    # Log activity start
    _log_activity(
        'profile_fetch_started',
        f"Starting profile fetch for {total_users} users in {channel.title}",
        source='worker_telegram',
        channel=channel,
    )

    if task_run:
        task_run.update_progress(f"Fetching profiles for {total_users} users", percent=0)

    try:
        service = TelegramService(account)

        async def do_fetch():
            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number
            )

            fetched = 0
            skipped = 0
            failed = 0

            try:
                for idx, user in enumerate(users):
                    # Check for cancellation
                    if task_run:
                        if await sync_to_async(task_run.check_cancelled)():
                            logger.info(f"Profile fetch cancelled for {channel.title}")
                            return {'cancelled': True, 'fetched': fetched, 'skipped': skipped}

                    # Skip users without access_hash
                    if not user.access_hash:
                        skipped += 1
                        continue

                    # Fetch full profile
                    result = await service.get_full_user(user.telegram_id, user.access_hash)

                    if result.get('success'):
                        data = result['data']

                        # Update user with full profile data
                        user.bio = data.get('bio', '')
                        user.birthday = data.get('birthday', '')
                        user.private_forward_name = data.get('private_forward_name', '')
                        user.personal_channel_id = data.get('personal_channel_id')
                        user.common_chats_count = data.get('common_chats_count', 0)
                        user.phone_calls_available = data.get('phone_calls_available', False)
                        user.video_calls_available = data.get('video_calls_available', False)
                        user.voice_messages_forbidden = data.get('voice_messages_forbidden', False)
                        user.contact_require_premium = data.get('contact_require_premium', False)
                        user.is_blocked = data.get('is_blocked', False)
                        user.business_intro = data.get('business_intro', '')
                        user.business_location = data.get('business_location', '')
                        user.business_work_hours = data.get('business_work_hours')
                        user.has_pinned_stories = data.get('has_pinned_stories', False)
                        user.has_scheduled_messages = data.get('has_scheduled_messages', False)
                        user.pinned_message_id = data.get('pinned_message_id')
                        user.full_profile_fetched_at = timezone.now()

                        await sync_to_async(user.save)()
                        fetched += 1

                        # Log individual fetch
                        if data.get('bio'):
                            await sync_to_async(_log_activity)(
                                'profile_fetched',
                                f"Fetched profile for {user.display_name}: bio={data['bio'][:50]}...",
                                source='worker_telegram',
                                channel=channel,
                                telegram_user=user,
                            )

                    elif 'flood_wait' in result:
                        # Handle flood wait
                        wait_seconds = result['flood_wait']
                        logger.warning(f"Flood wait during profile fetch: {wait_seconds}s")
                        account.flood_wait_until = timezone.now() + timezone.timedelta(seconds=wait_seconds)
                        await sync_to_async(account.save)(update_fields=['flood_wait_until'])
                        # Return partial progress, task will be rescheduled
                        return {
                            'flood_wait': wait_seconds,
                            'fetched': fetched,
                            'skipped': skipped,
                            'remaining': total_users - idx
                        }
                    else:
                        failed += 1
                        logger.debug(f"Failed to fetch profile for {user.telegram_id}: {result.get('error')}")

                    # Update progress
                    if task_run and (idx + 1) % 5 == 0:
                        percent = int(((idx + 1) / total_users) * 100)
                        await sync_to_async(task_run.update_progress)(
                            f"Fetched {fetched}/{total_users} profiles",
                            percent=percent
                        )

                    # Delay between requests to avoid rate limiting (2 seconds)
                    await asyncio.sleep(2)

                return {'success': True, 'fetched': fetched, 'skipped': skipped, 'failed': failed}

            finally:
                await service.disconnect()

        result = run_async(do_fetch())

        if result.get('cancelled'):
            if task_run:
                task_run.mark_cancelled()
            return result

        if result.get('flood_wait'):
            if task_run:
                task_run.update_progress(f"Flood wait, rescheduling. Fetched {result['fetched']} so far.")
            # Reschedule after flood wait
            fetch_user_profiles.send_with_options(
                args=(channel_id,),
                kwargs={'task_run_id': task_run_id},
                delay=result['flood_wait'] * 1000 + 5000
            )
            return result

        # Log completion
        _log_activity(
            'profile_fetch_completed',
            f"Completed profile fetch for {channel.title}: {result.get('fetched', 0)} fetched, {result.get('skipped', 0)} skipped",
            source='worker_telegram',
            channel=channel,
        )

        if task_run:
            task_run.update_progress(
                f"Completed: {result.get('fetched', 0)} fetched, {result.get('skipped', 0)} skipped",
                percent=100
            )
            task_run.mark_completed()

        return result

    except Exception as e:
        logger.exception(f"Error fetching user profiles for channel {channel_id}")
        if task_run:
            task_run.mark_failed(str(e)[:500])
        _log_activity(
            'error',
            f"Profile fetch failed for {channel.title}: {str(e)[:200]}",
            source='worker_telegram',
            channel=channel,
            error=str(e),
        )
        return {'success': False, 'error': str(e)}


@dramatiq.actor(queue_name=QUEUE_SCANS_MEMBERS, max_retries=2, min_backoff=30000)
def fetch_single_user_profile(user_id: int, channel_id: int):
    """
    Fetch full profile for a single user.
    """
    logger.info(f"fetch_single_user_profile: Starting for user {user_id}")

    try:
        user = TelegramUser.objects.get(pk=user_id)
    except TelegramUser.DoesNotExist:
        logger.error(f"User {user_id} not found")
        return {'success': False, 'error': 'User not found'}

    try:
        channel = TelegramChannel.objects.select_related('account').get(pk=channel_id)
    except TelegramChannel.DoesNotExist:
        logger.error(f"Channel {channel_id} not found")
        return {'success': False, 'error': 'Channel not found'}

    account = channel.account

    if not account.is_authenticated or not account.is_active:
        logger.warning(f"Account not active/authenticated")
        return {'success': False, 'error': 'Account not active'}

    if not user.access_hash:
        logger.warning(f"User {user_id} has no access_hash")
        return {'success': False, 'error': 'User has no access hash'}

    if account.is_flood_wait_active:
        logger.warning(f"Account is in flood wait, rescheduling")
        fetch_single_user_profile.send_with_options(
            args=(user_id, channel_id),
            delay=60000
        )
        return {'success': False, 'error': 'Account in flood wait, rescheduled'}

    try:
        service = TelegramService(account)

        async def do_fetch():
            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number
            )

            try:
                result = await service.get_full_user(user.telegram_id, user.access_hash)

                if result.get('success'):
                    data = result['data']
                    user.bio = data.get('bio', '')
                    user.birthday = data.get('birthday', '')
                    user.private_forward_name = data.get('private_forward_name', '')
                    user.personal_channel_id = data.get('personal_channel_id')
                    user.common_chats_count = data.get('common_chats_count', 0)
                    user.phone_calls_available = data.get('phone_calls_available', False)
                    user.video_calls_available = data.get('video_calls_available', False)
                    user.voice_messages_forbidden = data.get('voice_messages_forbidden', False)
                    user.contact_require_premium = data.get('contact_require_premium', False)
                    user.is_blocked = data.get('is_blocked', False)
                    user.business_intro = data.get('business_intro', '')
                    user.business_location = data.get('business_location', '')
                    user.business_work_hours = data.get('business_work_hours')
                    user.has_pinned_stories = data.get('has_pinned_stories', False)
                    user.has_scheduled_messages = data.get('has_scheduled_messages', False)
                    user.pinned_message_id = data.get('pinned_message_id')
                    user.full_profile_fetched_at = timezone.now()

                    await sync_to_async(user.save)()

                    if data.get('bio'):
                        await sync_to_async(_log_activity)(
                            'profile_fetched',
                            f"Fetched profile for {user.display_name}: bio={data['bio'][:50]}...",
                            source='worker_telegram',
                            channel=channel,
                            telegram_user=user,
                        )

                    return {'success': True, 'bio': data.get('bio', '')}
                else:
                    return result

            finally:
                await service.disconnect()

        result = run_async(do_fetch())
        logger.info(f"fetch_single_user_profile completed for user {user_id}: {result}")
        return result

    except Exception as e:
        logger.exception(f"Error fetching profile for user {user_id}")
        return {'success': False, 'error': str(e)}
