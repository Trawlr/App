"""
Scanning tasks for channel history and members.
"""

import asyncio
from datetime import timedelta
from pathlib import Path

import dramatiq
from asgiref.sync import sync_to_async
from django.utils import timezone
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import GetHistoryRequest
from telethon.tl.types import (
    ChannelParticipantAdmin,
    ChannelParticipantCreator,
    ChannelParticipantSelf,
)

from accounts.models import GlobalSettings, TelegramAccount
from accounts.telegram_service import TelegramService, run_async
from audit.models import TelegramChannel
from audit.user_tracking import download_user_profile_photo_async, track_user_from_message_async, track_user_from_participant_async
from downloads.consumers import sync_broadcast_queue_update
from downloads.models import ArchivedMessage, DownloadedFile, DownloadTask, TaskRun
from listeners.handlers import _async_create_message_entities, _extract_entities_data

from .base import (
    logger,
    QUEUE_SCANS_HISTORY,
    QUEUE_SCANS_MEMBERS,
    _log_activity,
    dispatch_task,
)


@dramatiq.actor(queue_name=QUEUE_SCANS_HISTORY)
def scan_channel_history(channel_id: int, task_run_id: str = None, skip_thumbnails: bool = False):
    """
    Scan a channel's full history and queue downloads.

    Args:
        skip_thumbnails: If True, skip downloading thumbnails regardless of channel config.
    """
    # Get TaskRun if provided
    task_run = None
    if task_run_id:
        try:
            task_run = TaskRun.objects.get(task_id=task_run_id)
            # Check if task was cancelled before we even start
            if task_run.should_cancel or task_run.status in ('cancelled', 'completed', 'failed'):
                logger.info(f"TaskRun {task_run_id} already cancelled/completed, skipping")
                return
            task_run.mark_running()
        except TaskRun.DoesNotExist:
            logger.warning(f"TaskRun {task_run_id} not found")

    try:
        channel = TelegramChannel.objects.select_related('account', 'config').get(pk=channel_id)
    except TelegramChannel.DoesNotExist:
        logger.error(f"Channel {channel_id} not found")
        if task_run:
            task_run.mark_failed("Channel not found")
        return

    # Skip inactive sources
    if not channel.active:
        logger.info(f"Channel {channel.title} is inactive, skipping history scan")
        if task_run:
            task_run.mark_completed()
        return

    # Log history scan started
    logger.info(f"scan_channel_history called with skip_thumbnails={skip_thumbnails}")
    _log_activity(
        'history_scan_started',
        f"Starting history scan for {channel.title}",
        source='worker_telegram',
        channel=channel,
    )

    config = channel.config
    account = channel.account
    logger.info(f"Config for {channel.title}: download_thumbnails={config.download_thumbnails}")

    if account.is_flood_wait_active:
        logger.warning(f"Account {account.phone_number} is in flood wait, delaying scan")
        if task_run:
            task_run.update_progress("Waiting for flood wait to clear")
        scan_channel_history.send_with_options(
            args=(channel_id,),
            kwargs={
                'task_run_id': task_run_id,
                'skip_thumbnails': skip_thumbnails,
            },
            delay=60000
        )
        return

    try:
        service = TelegramService(account)

        async def do_scan():
            nonlocal config

            # Use thread_sensitive=False to avoid async context detection issues with gevent
            def db_sync(func):
                return sync_to_async(func, thread_sensitive=False)

            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number
            )

            try:
                # Try to get entity, with fallback for private chats
                try:
                    entity = await service._client.get_entity(channel.telegram_id)
                except ValueError:
                    # Entity not cached - try to find it in dialogs first
                    logger.info(f"Entity {channel.telegram_id} not cached, fetching dialogs...")
                    await service._client.get_dialogs(limit=GlobalSettings.get_settings().dialog_cache_limit)
                    try:
                        entity = await service._client.get_entity(channel.telegram_id)
                    except ValueError:
                        logger.error(f"Could not find entity for channel {channel.telegram_id} ({channel.title})")
                        raise ValueError(f"Channel '{channel.title}' not accessible. Try syncing accounts first.")

                # Get total message count
                history = await service._client(GetHistoryRequest(
                    peer=entity,
                    offset_id=0,
                    offset_date=None,
                    add_offset=0,
                    limit=1,
                    max_id=0,
                    min_id=0,
                    hash=0
                ))
                # Handle different response types - some don't have .count
                config.total_messages = getattr(history, 'count', 0) or len(history.messages) if hasattr(history, 'messages') else 0
                await db_sync(config.save)()

                # Get storage root for thumbnails
                storage_root = await db_sync(lambda: GlobalSettings.get_settings().storage_root)()

                # Iterate through all messages
                tasks_created = 0
                messages_archived = 0
                users_tracked = 0
                thumbnails_downloaded = 0
                messages_processed = 0
                config_refresh_interval = 100  # Refresh config every N messages

                # Helper to refresh config from DB
                @db_sync
                def refresh_config():
                    channel.config.refresh_from_db()
                    return channel.config

                async for message in service._client.iter_messages(entity, reverse=True):
                    # Periodically refresh config to check for setting changes
                    messages_processed += 1
                    if messages_processed % config_refresh_interval == 0:
                        config = await refresh_config()
                        if config.is_paused:
                            logger.info(f"Scan paused by user for channel {channel.title}, stopping queue additions")
                            # Continue archiving but stop adding to download queue
                            # (we don't break here - we still want to archive messages)

                        # Check for cancellation
                        if task_run:
                            if await db_sync(task_run.check_cancelled)():
                                logger.info(f"Scan cancelled by user for channel {channel.title}")
                                return {
                                    'tasks': tasks_created,
                                    'messages': messages_archived,
                                    'users': users_tracked,
                                    'thumbnails': thumbnails_downloaded,
                                    'cancelled': True
                                }
                            # Update progress
                            total = config.total_messages or 1
                            percent = min(99, int((messages_processed / total) * 100))
                            await db_sync(task_run.update_progress)(
                                message=f"Processed {messages_processed:,} messages, {tasks_created} queued",
                                percent=percent,
                                data={'messages_processed': messages_processed, 'tasks_created': tasks_created}
                            )

                    # Skip if already processed (for incremental scans)
                    # Use sync_missing_media() to backfill downloads for older messages
                    if message.id <= config.last_downloaded_message_id:
                        continue

                    # Track user if sender is available
                    if message.sender:
                        _, created = await track_user_from_message_async(message.sender, channel, message.date)
                        if created:
                            users_tracked += 1

                    # Extract sender info
                    sender_name = ''
                    sender_username = ''
                    sender_id = None
                    if message.sender:
                        sender_id = message.sender_id
                        sender_name = getattr(message.sender, 'first_name', '') or ''
                        if hasattr(message.sender, 'last_name') and message.sender.last_name:
                            sender_name += f" {message.sender.last_name}"
                        sender_username = getattr(message.sender, 'username', '') or ''

                    # Determine media info
                    has_media = bool(message.media)
                    media_type = ''
                    file_id = ''
                    filename = ''
                    file_size = 0
                    mime_type = ''
                    thumbnail_path = ''

                    if message.photo:
                        media_type = 'photo'
                        filename = f"photo_{message.id}.jpg"
                        file_id = str(message.photo.id)
                    elif message.video:
                        media_type = 'video'
                        filename = getattr(message.video, 'file_name', '') or f"video_{message.id}.mp4"
                        file_size = message.video.size or 0
                        mime_type = message.video.mime_type or ''
                        file_id = str(message.video.id)
                    elif message.document:
                        media_type = 'file'
                        filename = getattr(message.document, 'file_name', '') or f"file_{message.id}"
                        file_size = message.document.size or 0
                        mime_type = message.document.mime_type or ''
                        file_id = str(message.document.id)

                    # Download thumbnail if enabled and message has media
                    # skip_thumbnails overrides config setting
                    should_download_thumb = config.download_thumbnails and not skip_thumbnails
                    if has_media and should_download_thumb:
                        try:
                            thumb_dir = Path(storage_root) / str(channel.telegram_id) / 'thumbnails'
                            thumb_dir.mkdir(parents=True, exist_ok=True)
                            thumb_filename = f"thumb_{message.id}.jpg"
                            thumb_path = thumb_dir / thumb_filename

                            # Skip if thumbnail already exists
                            if thumb_path.exists():
                                logger.info(f"Message {message.id}: thumbnail already exists at {thumb_path}")
                                thumbnail_path = str(Path(str(channel.telegram_id)) / 'thumbnails' / thumb_filename)
                            else:
                                logger.info(f"Message {message.id}: attempting thumbnail download to {thumb_path}")
                                thumb_downloaded = None
                                if message.photo:
                                    # For photos, download the smallest size using thumb index
                                    sizes = message.photo.sizes
                                    if sizes:
                                        smallest = min(range(len(sizes)),
                                                       key=lambda i: getattr(sizes[i], 'size', 0) or
                                                                    (getattr(sizes[i], 'w', 0) * getattr(sizes[i], 'h', 0)))
                                        thumb_downloaded = await service._client.download_media(
                                            message,
                                            file=str(thumb_path),
                                            thumb=smallest  # Index into photo.sizes
                                        )
                                else:
                                    # For videos/documents, use thumb=-1 to get thumbnail
                                    thumb_downloaded = await service._client.download_media(
                                        message,
                                        file=str(thumb_path),
                                        thumb=-1
                                    )

                                # Verify file was actually downloaded with content
                                if thumb_downloaded and thumb_path.exists() and thumb_path.stat().st_size > 0:
                                    logger.info(f"Message {message.id}: thumbnail downloaded successfully")
                                    thumbnail_path = str(Path(str(channel.telegram_id)) / 'thumbnails' / thumb_filename)
                                    thumbnails_downloaded += 1
                                else:
                                    # Clean up empty file if created
                                    if thumb_path.exists() and thumb_path.stat().st_size == 0:
                                        thumb_path.unlink()
                                        logger.info(f"Message {message.id}: deleted empty thumbnail file")
                                    else:
                                        logger.info(f"Message {message.id}: download_media returned None (no thumbnail available)")
                        except Exception as e:
                            logger.info(f"Could not download thumbnail for message {message.id}: {e}")

                    # Archive ALL messages (text and/or media)
                    archived_msg, created = await db_sync(ArchivedMessage.objects.get_or_create)(
                        channel=channel,
                        message_id=message.id,
                        defaults={
                            'text': message.text or '',
                            'has_media': has_media,
                            'media_type': media_type,
                            'telegram_file_id': file_id,
                            'original_filename': filename,
                            'file_size': file_size,
                            'mime_type': mime_type,
                            'thumbnail_path': thumbnail_path,
                            'sender_id': sender_id,
                            'sender_name': sender_name,
                            'sender_username': sender_username,
                            'reply_to_message_id': message.reply_to_msg_id if message.reply_to else None,
                            'views': message.views or 0,
                            'forwards': message.forwards or 0,
                            'telegram_date': message.date,
                            'edited_date': message.edit_date,
                        }
                    )
                    messages_archived += 1

                    # Extract and store message entities (URLs, mentions, hashtags, etc.)
                    # Use message.message (plain text) - entity offsets are relative to plain text
                    if created and message.entities:
                        entities_data = _extract_entities_data(message.message or '', message.entities)
                        if entities_data:
                            await _async_create_message_entities(archived_msg, entities_data)

                    # Queue media downloads if auto_download is enabled and not paused
                    # Re-check config values (may have been refreshed) before queuing
                    if has_media and config.auto_download_enabled and not config.is_paused:
                        should_download = False
                        if media_type == 'photo' and config.download_photos:
                            should_download = True
                        elif media_type == 'video' and config.download_videos:
                            should_download = True
                        elif media_type == 'file' and config.download_files:
                            should_download = True

                        if should_download:
                            # Skip if already downloaded
                            already_downloaded = await db_sync(
                                lambda: DownloadedFile.objects.filter(channel=channel, message_id=message.id).exists()
                            )()

                            if not already_downloaded:
                                effective_priority = config.priority + config.get_file_type_priority(media_type)
                                await db_sync(DownloadTask.objects.get_or_create)(
                                    channel=channel,
                                    message_id=message.id,
                                    defaults={
                                        'telegram_file_id': file_id,
                                        'original_filename': filename,
                                        'file_type': media_type,
                                        'file_size': file_size,
                                        'mime_type': mime_type,
                                        'priority': effective_priority,
                                        'max_retries': 3,
                                        'pending_reason': 'queued',
                                    }
                                )
                                tasks_created += 1

                return {
                    'tasks': tasks_created,
                    'messages': messages_archived,
                    'users': users_tracked,
                    'thumbnails': thumbnails_downloaded
                }
            finally:
                # Always disconnect to prevent "Task was destroyed but pending" errors
                await service.disconnect()

        result = run_async(do_scan())

        # Check if cancelled
        if result.get('cancelled'):
            logger.info(f"Scan was cancelled for {channel.title}")
            if task_run:
                task_run.mark_cancelled()
            _log_activity(
                'history_scan_cancelled',
                f"History scan cancelled for {channel.title}",
                source='worker_telegram',
                channel=channel,
                tasks_created=result['tasks'],
                messages_archived=result['messages'],
            )
            return

        logger.info(
            f"Scan complete for {channel.title}: {result['tasks']} tasks, "
            f"{result['messages']} messages, {result['users']} new users, "
            f"{result['thumbnails']} thumbnails"
        )

        # Mark TaskRun as completed
        if task_run:
            task_run.mark_completed()

        # Log history scan completed
        _log_activity(
            'history_scan_completed',
            f"History scan complete for {channel.title}",
            source='worker_telegram',
            channel=channel,
            tasks_created=result['tasks'],
            messages_archived=result['messages'],
            users_tracked=result['users'],
            thumbnails_downloaded=result['thumbnails'],
        )

        # Broadcast queue update if tasks were created
        if result['tasks'] > 0:
            try:
                user_id = account.user_id
                sync_broadcast_queue_update(user_id, 'added', stats={'new_tasks': result['tasks']})
            except Exception as broadcast_err:
                logger.debug(f"Failed to broadcast queue update: {broadcast_err}")

        # Auto-queue member scan for groups/supergroups after history scan
        # This captures all existing members who haven't sent messages
        if channel.channel_type in ('group', 'supergroup'):
            try:
                dispatch_task(
                    scan_channel_members,
                    task_type='scan_members',
                    channel=channel,
                    account=account,
                    args=(channel.pk,),
                    kwargs={'skip_profile_photos': True},
                )
                logger.info(f"Auto-queued member scan for {channel.title} after history scan")
                _log_activity(
                    'member_scan_queued',
                    f"Auto-queued member scan for {channel.title} after history scan",
                    source='worker_telegram',
                    channel=channel,
                )
            except Exception as member_err:
                logger.warning(f"Failed to queue member scan for {channel.title}: {member_err}")

    except Exception as e:
        # Check if it's a FloodWaitError - reschedule instead of failing
        if isinstance(e, FloodWaitError) or 'FloodWaitError' in type(e).__name__:
            wait_seconds = getattr(e, 'seconds', 60)
            logger.warning(f"FloodWaitError for channel {channel_id}: must wait {wait_seconds}s")

            # Update account flood wait status
            account.flood_wait_until = timezone.now() + timedelta(seconds=wait_seconds + 5)
            account.save(update_fields=['flood_wait_until'])

            if task_run:
                task_run.update_progress(f"Flood wait: rescheduled in {wait_seconds}s")

            # Reschedule the task to run after the wait period
            scan_channel_history.send_with_options(
                args=(channel_id,),
                kwargs={
                    'task_run_id': task_run_id,
                    'skip_thumbnails': skip_thumbnails,
                },
                delay=(wait_seconds + 10) * 1000
            )

            _log_activity(
                'history_scan_rescheduled',
                f"History scan rescheduled for {channel.title} due to flood wait ({wait_seconds}s)",
                source='worker_telegram',
                channel=channel,
            )
            return {'error': f'FloodWait: rescheduled in {wait_seconds}s'}

        logger.exception(f"Error scanning channel {channel_id}")
        # Mark TaskRun as failed
        if task_run:
            task_run.mark_failed(str(e)[:500])
        # Log error
        _log_activity(
            'error',
            f"History scan failed for {channel.title}: {str(e)[:200]}",
            source='worker_telegram',
            channel=channel,
            error=str(e),
        )


@dramatiq.actor(queue_name=QUEUE_SCANS_MEMBERS)
def scan_channel_members(channel_id: int, task_run_id: str = None, skip_profile_photos: bool = False):
    """
    Scan a channel's member list and track users.
    Also downloads profile photos for all members (unless skip_profile_photos is True).
    Only works for groups/supergroups where the bot has admin access.
    """
    logger.info(f"scan_channel_members: Starting member scan for channel {channel_id} (skip_photos={skip_profile_photos})")

    # Get TaskRun if provided
    task_run = None
    if task_run_id:
        try:
            task_run = TaskRun.objects.get(task_id=task_run_id)
            # Check if task was cancelled before we even start
            if task_run.should_cancel or task_run.status in ('cancelled', 'completed', 'failed'):
                logger.info(f"TaskRun {task_run_id} already cancelled/completed, skipping")
                return
            task_run.mark_running()
        except TaskRun.DoesNotExist:
            logger.warning(f"TaskRun {task_run_id} not found")

    try:
        channel = TelegramChannel.objects.select_related('account').get(pk=channel_id)
        logger.info(f"scan_channel_members: Found channel '{channel.title}' (id={channel_id}, type={channel.channel_type})")
    except TelegramChannel.DoesNotExist:
        logger.error(f"Channel {channel_id} not found")
        if task_run:
            task_run.mark_failed("Channel not found")
        return

    # Skip inactive sources
    if not channel.active:
        logger.info(f"Channel {channel.title} is inactive, skipping member scan")
        if task_run:
            task_run.mark_completed()
        return

    # Skip non-group channels - member scanning only works for groups/supergroups
    if channel.channel_type not in ('group', 'supergroup'):
        logger.info(f"Skipping member scan for {channel.title} - not a group (type={channel.channel_type})")
        if task_run:
            task_run.mark_completed()
        return

    # Log member scan started
    _log_activity(
        'member_scan_started',
        f"Starting member scan for {channel.title}",
        source='worker_telegram',
        channel=channel,
    )

    account = channel.account

    if account.is_flood_wait_active:
        logger.warning(f"Account {account.phone_number} is in flood wait, delaying member scan")
        if task_run:
            task_run.update_progress("Waiting for flood wait to clear")
        scan_channel_members.send_with_options(args=(channel_id,), kwargs={'task_run_id': task_run_id}, delay=60000)
        return

    try:
        service = TelegramService(account)

        async def do_scan():
            logger.info(f"scan_channel_members: Creating Telegram client for {channel.title}")
            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number
            )

            try:
                logger.info(f"scan_channel_members: Client created, getting entity")

                # Get entity - with better error handling
                entity = None
                try:
                    entity = await service._client.get_entity(channel.telegram_id)
                except ValueError as e:
                    # Entity not in cache, try fetching dialogs first
                    logger.info(f"Entity {channel.telegram_id} not cached, fetching dialogs...")
                    try:
                        await service._client.get_dialogs(limit=GlobalSettings.get_settings().dialog_cache_limit)
                        entity = await service._client.get_entity(channel.telegram_id)
                    except Exception as dialog_err:
                        logger.warning(f"Failed to fetch dialogs for {channel.title}: {dialog_err}")
                        return {'error': f'Could not access channel: {e}'}

                if entity is None:
                    logger.warning(f"Could not get entity for channel {channel.title}")
                    return {'error': 'Could not get channel entity'}

                logger.info(f"scan_channel_members: Got entity, starting participant scan")

                users_tracked = 0
                admins_found = 0
                photos_downloaded = 0
                total_processed = 0

                # Try to get participants (requires admin rights for large groups)
                try:
                    async for participant in service._client.iter_participants(entity):
                        total_processed += 1
                        is_admin = False
                        is_creator = False
                        admin_title = ''

                        # Check participant type for admin/creator status
                        if hasattr(participant, 'participant'):
                            p = participant.participant
                            if isinstance(p, ChannelParticipantCreator):
                                is_creator = True
                                is_admin = True
                                admin_title = getattr(p, 'rank', '') or 'Creator'
                                admins_found += 1
                            elif isinstance(p, ChannelParticipantAdmin):
                                is_admin = True
                                admin_title = getattr(p, 'rank', '') or 'Admin'
                                admins_found += 1

                        user, created = await track_user_from_participant_async(
                            participant, channel, is_admin, is_creator, admin_title
                        )
                        if created:
                            users_tracked += 1

                        # Download profile photo for the user (if enabled and not skipped)
                        if user and account.download_profile_photos and not skip_profile_photos:
                            photo_downloaded = await download_user_profile_photo_async(
                                service._client, participant.id, user, sender=participant
                            )
                            if photo_downloaded:
                                photos_downloaded += 1

                        # Log progress every 25 users and check for cancellation
                        if total_processed % 25 == 0:
                            logger.info(
                                f"scan_channel_members: {channel.title} progress - "
                                f"{total_processed} processed, {users_tracked} new, {photos_downloaded} photos"
                            )
                            # Check for cancellation and update progress
                            if task_run:
                                def db_sync(func):
                                    return sync_to_async(func, thread_sensitive=False)
                                if await db_sync(task_run.check_cancelled)():
                                    logger.info(f"Member scan cancelled for {channel.title}")
                                    return {
                                        'users': users_tracked,
                                        'admins': admins_found,
                                        'photos': photos_downloaded,
                                        'cancelled': True
                                    }
                                await db_sync(task_run.update_progress)(
                                    message=f"Scanned {total_processed} members, {users_tracked} new",
                                    data={'total_processed': total_processed, 'users_tracked': users_tracked}
                                )

                except Exception as e:
                    logger.warning(f"Could not get participants for {channel.title}: {e}")
                    # May not have permission - that's okay

                logger.info(f"scan_channel_members: Returning results")
                return {'users': users_tracked, 'admins': admins_found, 'photos': photos_downloaded}
            finally:
                # Always disconnect to prevent "Task was destroyed but pending" errors
                await service.disconnect()
                logger.info(f"scan_channel_members: Disconnected")

        result = run_async(do_scan())

        # Check if cancelled
        if result.get('cancelled'):
            logger.info(f"Member scan was cancelled for {channel.title}")
            if task_run:
                task_run.mark_cancelled()
            _log_activity(
                'member_scan_cancelled',
                f"Member scan cancelled for {channel.title}",
                source='worker_telegram',
                channel=channel,
                users_tracked=result.get('users', 0),
            )
            return result

        logger.info(
            f"Member scan complete for {channel.title}: {result['users']} new users, "
            f"{result['admins']} admins, {result['photos']} photos downloaded"
        )

        # Mark TaskRun as completed
        if task_run:
            task_run.mark_completed()

        # Log member scan completed
        _log_activity(
            'member_scan_completed',
            f"Member scan complete for {channel.title}",
            source='worker_telegram',
            channel=channel,
            users_tracked=result['users'],
            admins_found=result['admins'],
            photos_downloaded=result['photos'],
        )

        return result

    except Exception as e:
        # Check if it's a FloodWaitError
        if isinstance(e, FloodWaitError) or 'FloodWaitError' in type(e).__name__:
            wait_seconds = getattr(e, 'seconds', 60)
            logger.warning(f"FloodWaitError for channel {channel_id}: must wait {wait_seconds}s")

            # Update account flood wait status
            account.flood_wait_until = timezone.now() + timedelta(seconds=wait_seconds + 5)
            account.save(update_fields=['flood_wait_until'])

            if task_run:
                task_run.update_progress(f"Flood wait: rescheduled in {wait_seconds}s")

            # Reschedule the task to run after the wait period
            scan_channel_members.send_with_options(args=(channel_id,), kwargs={'task_run_id': task_run_id}, delay=(wait_seconds + 10) * 1000)
            return {'error': f'FloodWait: rescheduled in {wait_seconds}s'}

        logger.exception(f"Error scanning members for channel {channel_id}")
        # Mark TaskRun as failed
        if task_run:
            task_run.mark_failed(str(e)[:500])


@dramatiq.actor(queue_name=QUEUE_SCANS_MEMBERS)
def scan_all_channel_members_for_user(task_run_id: str = None):
    """
    Scan members for all channels belonging to a user.
    Uses a single client connection per account to avoid flooding Telegram's servers.
    """
    logger.info(f"scan_all_channel_members_for_user: Starting channel scan")

    # Get TaskRun if provided
    task_run = None
    if task_run_id:
        try:
            task_run = TaskRun.objects.get(task_id=task_run_id)
            if task_run.should_cancel or task_run.status in ('cancelled', 'completed', 'failed'):
                logger.info(f"TaskRun {task_run_id} already cancelled/completed, skipping")
                return
            task_run.mark_running()
        except TaskRun.DoesNotExist:
            logger.warning(f"TaskRun {task_run_id} not found")

    _log_activity(
        'member_scan_started',
        "Starting periodic member scan for all groups",
        source='worker_telegram',
    )

    # Get all accounts for this user
    accounts = TelegramAccount.objects.filter(is_active=True, is_authenticated=True)

    if not accounts.exists():
        logger.warning(f"No active accounts found")
        if task_run:
            task_run.mark_completed()
        _log_activity(
            'member_scan_completed',
            "Periodic member scan completed: no active accounts",
            source='worker_telegram',
        )
        return {'total_users': 0, 'total_photos': 0, 'channels_scanned': 0}

    total_users = 0
    total_photos = 0
    channels_scanned = 0

    for account in accounts:
        if account.is_flood_wait_active:
            logger.warning(f"Account {account.phone_number} is in flood wait, skipping")
            continue

        # Get only active groups/supergroups for this account (member scanning doesn't work for other types)
        channels = list(TelegramChannel.objects.filter(
            account=account,
            active=True,
            channel_type__in=['group', 'supergroup']
        ))

        if not channels:
            logger.info(f"No groups found for account {account.phone_number}")
            continue

        try:
            service = TelegramService(account)

            async def scan_all_channels():
                nonlocal total_users, total_photos, channels_scanned

                await service.create_client(
                    account.api_id,
                    account.api_hash,
                    account.phone_number
                )

                try:
                    # Get dialogs once to populate entity cache
                    global_settings = GlobalSettings.get_settings()
                    logger.info(f"Fetching dialogs for account {account.phone_number}")
                    await service._client.get_dialogs(limit=global_settings.dialog_cache_limit)

                    for channel in channels:
                        try:
                            logger.info(f"Scanning members for {channel.title}")

                            # Get entity
                            try:
                                entity = await service._client.get_entity(channel.telegram_id)
                            except ValueError:
                                logger.warning(f"Could not get entity for {channel.title}, skipping")
                                continue

                            users_in_channel = 0
                            photos_in_channel = 0

                            try:
                                async for participant in service._client.iter_participants(entity):
                                    is_admin = False
                                    is_creator = False
                                    admin_title = ''

                                    if hasattr(participant, 'participant'):
                                        p = participant.participant
                                        if isinstance(p, ChannelParticipantCreator):
                                            is_creator = True
                                            is_admin = True
                                            admin_title = getattr(p, 'rank', '') or 'Creator'
                                        elif isinstance(p, ChannelParticipantAdmin):
                                            is_admin = True
                                            admin_title = getattr(p, 'rank', '') or 'Admin'

                                    user, created = await track_user_from_participant_async(
                                        participant, channel, is_admin, is_creator, admin_title
                                    )
                                    if created:
                                        users_in_channel += 1

                                    # Download profile photo (if enabled for this account)
                                    if user and account.download_profile_photos:
                                        try:
                                            photo_downloaded = await download_user_profile_photo_async(
                                                service._client, participant.id, user, sender=participant
                                            )
                                            if photo_downloaded:
                                                photos_in_channel += 1
                                        except FloodWaitError as photo_err:
                                            # Wait for the required time before continuing
                                            wait_seconds = getattr(photo_err, 'seconds', 60)
                                            logger.warning(f"FloodWaitError downloading photo for user {participant.id}: waiting {wait_seconds}s")
                                            await asyncio.sleep(wait_seconds + 5)
                                            # Don't retry this photo, just continue to next participant

                            except FloodWaitError as e:
                                # FloodWait during participant iteration
                                wait_seconds = getattr(e, 'seconds', 60)
                                logger.warning(f"FloodWaitError scanning {channel.title}: waiting {wait_seconds}s")
                                # Update account and wait
                                account.flood_wait_until = timezone.now() + timedelta(seconds=wait_seconds + 5)
                                await sync_to_async(account.save, thread_sensitive=True)(update_fields=['flood_wait_until'])
                                await asyncio.sleep(wait_seconds + 5)
                            except Exception as e:
                                logger.warning(f"Could not get participants for {channel.title}: {e}")

                            total_users += users_in_channel
                            total_photos += photos_in_channel
                            channels_scanned += 1

                            logger.info(f"Scanned {channel.title}: {users_in_channel} new users, {photos_in_channel} photos")

                            # Small delay between channels to avoid rate limiting
                            await asyncio.sleep(1)

                        except Exception as e:
                            logger.warning(f"Error scanning channel {channel.title}: {e}")
                            continue

                finally:
                    await service.disconnect()

            run_async(scan_all_channels())

        except Exception as e:
            logger.exception(f"Error scanning channels for account {account.phone_number}: {e}")

    logger.info(f"scan_all_channel_members_for_user complete: {channels_scanned} channels, {total_users} new users, {total_photos} photos")
    return {
        'total_users': total_users,
        'total_photos': total_photos,
        'channels_scanned': channels_scanned,
    }
