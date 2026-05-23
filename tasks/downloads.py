"""
Download-related tasks for downloading files, thumbnails, and profile photos.
"""

import asyncio
import base64
import hashlib
import os
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

import dramatiq
from asgiref.sync import sync_to_async
from django.db.models import Q
from django.utils import timezone
from dramatiq import Retry
from telethon.errors import FloodWaitError
from telethon.tl.types import User

from accounts.client_pool import client_pool
from accounts.models import GlobalSettings, TelegramAccount
from accounts.telegram_service import TelegramService, run_async
from audit.models import TelegramChannel, TelegramUser
from downloads.consumers import sync_broadcast_progress, sync_broadcast_queue_update, sync_broadcast_status_change
from downloads.models import ArchivedMessage, DownloadedFile, DownloadTask, TaskRun
from storage.utils import get_storage_backend, is_cloud_backend

from .base import (
    logger,
    QUEUE_DEFAULT,
    QUEUE_DOWNLOADS,
    QUEUE_SCANS_HISTORY,
    _log_activity,
)


@dramatiq.actor(queue_name=QUEUE_DOWNLOADS, max_retries=3, min_backoff=60000, max_backoff=300000)
def download_file(task_id: int):
    """
    Download a single file from Telegram.
    """
    try:
        task = DownloadTask.objects.select_related('channel', 'channel__account').get(pk=task_id)
    except DownloadTask.DoesNotExist:
        logger.error(f"DownloadTask {task_id} not found")
        return

    # Skip tasks that are already completed, downloading, or paused (prevents duplicate processing)
    if task.status in ('completed', 'paused'):
        logger.debug(f"Task {task_id} already {task.status}, skipping")
        return
    if task.status == 'downloading':
        logger.debug(f"Task {task_id} already downloading, skipping duplicate dispatch")
        return

    account = task.channel.account
    global_settings = GlobalSettings.get_settings()

    # Check for duplicate by file_unique_id BEFORE downloading (global deduplication)
    # Exclude files marked as deleted — their physical data may no longer exist in storage
    if global_settings.file_deduplication_enabled and task.file_unique_id:
        existing_file = DownloadedFile.objects.filter(
            file_unique_id=task.file_unique_id,
            is_duplicate=False,
            deleted_from_disk=False,
            file_path__gt='',
        ).first()

        # Verify the original file actually exists in storage (not just in DB)
        if existing_file:
            backend = get_storage_backend(global_settings)
            if not backend.file_exists(existing_file.file_path):
                logger.info(
                    f"Dedup candidate pk={existing_file.pk} not found in storage, "
                    f"downloading fresh copy"
                )
                existing_file = None

        if existing_file:
            # Get media dimensions from the ArchivedMessage
            archived_msg = ArchivedMessage.objects.filter(
                channel_id=task.channel_id, message_id=task.message_id
            ).values('media_width', 'media_height', 'media_duration').first()

            # Create duplicate record pointing to existing file (get_or_create to handle retries/races)
            downloaded_file, created = DownloadedFile.objects.get_or_create(
                task=task,
                defaults={
                    'channel': task.channel,
                    'message_id': task.message_id,
                    'original_filename': task.original_filename,
                    'stored_filename': '',  # No file on disk for duplicate
                    'file_path': '',
                    'file_type': task.file_type,
                    'file_size': existing_file.file_size,
                    'mime_type': task.mime_type,
                    'sha256_hash': existing_file.sha256_hash,  # Copy from original
                    'is_duplicate': True,
                    'original_file': existing_file,
                    'telegram_file_id': task.telegram_file_id,
                    'file_unique_id': task.file_unique_id,
                    'telegram_date': existing_file.telegram_date,
                    'media_width': archived_msg.get('media_width') if archived_msg else None,
                    'media_height': archived_msg.get('media_height') if archived_msg else None,
                    'media_duration': archived_msg.get('media_duration') if archived_msg else None,
                },
            )
            if not created:
                logger.info(f"DownloadedFile already exists for task {task_id} (duplicate path), skipping")

            # Link ArchivedMessage to this DownloadedFile
            ArchivedMessage.objects.filter(
                channel_id=task.channel_id,
                message_id=task.message_id
            ).update(downloaded_file=downloaded_file)

            # Mark task as completed
            task.status = 'completed'
            task.progress = 100
            task.completed_at = timezone.now()
            task.save()

            logger.info(f"Skipped download (duplicate by file_unique_id): {task.original_filename} -> {existing_file.original_filename}")

            _log_activity(
                'download_completed',
                f"Skipped duplicate {task.file_type}: {task.original_filename}",
                channel=task.channel,
                filename=task.original_filename,
                file_type=task.file_type,
                file_size=existing_file.file_size,
                is_duplicate=True,
                task_id=task_id,
            )

            # Broadcast completion
            user_id = account.user_id
            try:
                sync_broadcast_status_change(user_id, task_id, 'completed', task.original_filename)
            except Exception as e:
                logger.debug(f"WebSocket broadcast error: {e}")

            return

    # Check flood wait - reset to pending instead of retrying (to avoid exhausting retries)
    if account.is_flood_wait_active:
        logger.warning(f"Account {account.phone_number} is in flood wait, resetting task to pending")
        task.status = 'pending'
        task.pending_reason = 'account_flood_wait'
        task.save(update_fields=['status', 'pending_reason'])
        return  # Let process_download_queue handle it when flood wait ends

    # Atomically claim the task. When a worker restarts, StartupRecoveryMiddleware resets
    # stuck tasks to 'pending', but their old Dramatiq messages are still in RabbitMQ.
    # process_download_queue then dispatches them again, creating duplicate messages.
    # Both messages race here — the atomic UPDATE ensures only one wins.
    claimed = DownloadTask.objects.filter(
        pk=task_id,
        status__in=['pending', 'failed'],
    ).update(status='downloading', started_at=timezone.now())
    if not claimed:
        logger.debug(f"Task {task_id} already claimed by another worker, skipping")
        return
    task.refresh_from_db()

    # Log download started
    _log_activity(
        'download_started',
        f"Downloading {task.file_type}: {task.original_filename}",
        channel=task.channel,
        filename=task.original_filename,
        file_type=task.file_type,
        file_size=task.file_size,
        task_id=task_id,
    )

    # Get user ID for WebSocket broadcasting
    user_id = account.user_id

    # Broadcast status change to 'downloading' to trigger UI update
    # This ensures the pending row becomes a downloading row with progress bar
    try:
        sync_broadcast_status_change(user_id, task_id, 'downloading', task.original_filename)
    except Exception as e:
        logger.debug(f"Initial status broadcast error: {e}")

    try:
        # Track for rate-limited progress updates
        last_broadcast_time = [0]
        last_progress = [0]  # Track last broadcasted progress to avoid duplicates
        broadcast_interval = 0.3  # Broadcast at most every 0.3 seconds for smoother updates

        # Generate filename based on format (before do_download so vars are in outer scope)
        original_ext = Path(task.original_filename).suffix if task.original_filename else ''
        if global_settings.filename_format == 'guid':
            filename = f"{uuid.uuid4()}{original_ext}"
        elif global_settings.filename_format == 'timestamp':
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            safe_name = "".join(c for c in task.original_filename if c.isalnum() or c in '._-')[:50]
            filename = f"{timestamp}_{safe_name}" if safe_name else f"{timestamp}{original_ext}"
        elif global_settings.filename_format == 'file_id':
            filename = f"{task.telegram_file_id[:50]}{original_ext}"
        else:  # message_id
            safe_name = "".join(c for c in task.original_filename if c.isalnum() or c in '._-')[:50]
            filename = f"{task.message_id}_{safe_name}" if safe_name else f"{task.message_id}{original_ext}"

        # Build the relative path (used as object key for cloud, path suffix for local)
        # Always use forward slashes for consistent storage keys across platforms
        relative_path = f"{task.channel.telegram_id}/{task.file_type}/{filename}"

        # Determine download location: direct to storage_root for local, temp dir for cloud
        use_cloud = is_cloud_backend(global_settings)
        backend = get_storage_backend(global_settings)

        if use_cloud:
            temp_dir = Path(tempfile.gettempdir()) / 'trawlr-downloads'
            temp_dir.mkdir(parents=True, exist_ok=True)
            file_path = temp_dir / filename
        else:
            channel_dir = Path(global_settings.storage_root) / str(task.channel.telegram_id) / task.file_type
            channel_dir.mkdir(parents=True, exist_ok=True)
            file_path = channel_dir / filename

        async def do_download(client):
            # Get the entity with fallback for private chats
            try:
                entity = await client.get_entity(task.channel.telegram_id)
            except ValueError:
                # Entity not cached - try to find it in dialogs first
                logger.info(f"Entity {task.channel.telegram_id} not cached, fetching dialogs...")
                await client.get_dialogs(limit=GlobalSettings.get_settings().dialog_cache_limit)
                entity = await client.get_entity(task.channel.telegram_id)
            message = await client.get_messages(entity, ids=task.message_id)

            if not message:
                logger.warning(f"Message {task.message_id} not found in channel {task.channel.telegram_id}")
                return {'error': 'Message not found - may have been deleted', 'unavailable': True}

            if not message.media:
                logger.warning(
                    f"Message {task.message_id} has no media. "
                    f"Text: {(message.text or '')[:100]!r}, "
                    f"Type: {type(message).__name__}"
                )
                return {'error': 'Message has no media - may have been edited', 'unavailable': True}

            # Track download speed - separate from broadcast timing
            speed_last_bytes = [0]
            speed_last_time = [time.time()]
            current_speed = [0]  # Persistent speed value

            # Stall detection: abort if no new bytes arrive for this long.
            # Does NOT limit total download time, only detects frozen connections.
            stall_timeout = 300  # 5 minutes of zero progress = hung DC connection
            stall_last_bytes = [0]
            stall_last_advance = [time.time()]

            # Download with progress callback
            # Note: callback runs in async context, can't do sync DB saves
            callback_count = [0]

            def progress_callback(current, total):
                callback_count[0] += 1
                current_time = time.time()

                # Stall detection: update advance time whenever bytes increase,
                # regardless of total (total=0 when Telegram doesn't report file size).
                if current > stall_last_bytes[0]:
                    stall_last_bytes[0] = current
                    stall_last_advance[0] = current_time

                if total > 0:
                    progress = int((current / total) * 100)

                    # Calculate speed based on bytes since last speed calculation
                    speed_time_diff = current_time - speed_last_time[0]
                    if speed_time_diff >= 0.2:  # Update speed every 0.2 seconds
                        bytes_diff = current - speed_last_bytes[0]
                        if speed_time_diff > 0:
                            current_speed[0] = int(bytes_diff / speed_time_diff)
                        speed_last_bytes[0] = current
                        speed_last_time[0] = current_time

                    # Rate-limited WebSocket broadcast
                    # Broadcast if: enough time passed OR progress changed significantly
                    time_since_broadcast = current_time - last_broadcast_time[0]
                    progress_change = progress - last_progress[0]

                    should_broadcast = (
                        time_since_broadcast >= broadcast_interval or
                        progress_change >= 5 or  # Broadcast every 5% change
                        progress == 100  # Always broadcast completion
                    )

                    if should_broadcast:
                        try:
                            sync_broadcast_progress(
                                user_id, task_id, progress, current, total, current_speed[0]
                            )
                            last_progress[0] = progress
                            last_broadcast_time[0] = current_time
                        except Exception as e:
                            logger.warning(f"WebSocket broadcast error: {e}")

            # Retry loop for DC migration auth errors
            max_dc_retries = 2
            dc_retry_count = 0
            downloaded_path = None

            while dc_retry_count <= max_dc_retries:
                try:
                    # Wrap in a Task so the stall watchdog can cancel it.
                    # asyncio.CancelledError from the watchdog propagates here.
                    download_task = asyncio.ensure_future(
                        client.download_media(
                            message,
                            file=str(file_path),
                            progress_callback=progress_callback
                        )
                    )

                    async def stall_watchdog():
                        """Cancel the download if no bytes advance for stall_timeout."""
                        while not download_task.done():
                            await asyncio.sleep(30)
                            elapsed = time.time() - stall_last_advance[0]
                            if elapsed > stall_timeout and not download_task.done():
                                logger.warning(
                                    f"Cancelling stalled download for task {task_id} "
                                    f"({task.original_filename}): no progress for {elapsed:.0f}s"
                                )
                                download_task.cancel()
                                return

                    watchdog_task = asyncio.ensure_future(stall_watchdog())
                    try:
                        downloaded_path = await download_task
                    except asyncio.CancelledError:
                        # Clean up partial file
                        try:
                            if file_path.exists():
                                file_path.unlink()
                        except Exception:
                            pass
                        # Return sentinel only — do NOT reset the task to pending here.
                        # The reset must happen after client_pool.execute() returns so the
                        # OS thread is back in the pool before the queue processor sees the
                        # freed slot. Resetting here (inside the thread) causes the queue
                        # processor to dispatch a replacement while this thread is still
                        # occupied, resulting in slot count creep (+1 per stall cycle).
                        return {'stalled': True}
                    finally:
                        watchdog_task.cancel()

                    break  # Success, exit retry loop
                except Exception as download_err:
                    # Check for AuthBytesInvalidError (DC migration auth failure)
                    err_name = type(download_err).__name__
                    if 'AuthBytesInvalid' in err_name or 'AuthBytesInvalidError' in str(type(download_err)):
                        dc_retry_count += 1
                        if dc_retry_count <= max_dc_retries:
                            logger.warning(
                                f"AuthBytesInvalidError on task {task_id}, "
                                f"clearing DC sessions and retrying ({dc_retry_count}/{max_dc_retries})"
                            )
                            # Clear exported senders cache to force re-auth to other DCs
                            if hasattr(client, '_exported_sessions'):
                                client._exported_sessions.clear()
                            if hasattr(client, '_borrowed_senders'):
                                # Return all borrowed senders
                                for dc_id in list(client._borrowed_senders.keys()):
                                    try:
                                        await client._return_exported_sender(dc_id)
                                    except Exception:
                                        pass
                                client._borrowed_senders.clear()
                            # Continue to retry immediately
                            continue
                        else:
                            raise  # Exhausted retries
                    else:
                        raise  # Not an auth error, re-raise

            logger.info(f"Download complete for task={task_id}, total callbacks={callback_count[0]}")

            if downloaded_path:
                return {
                    'path': downloaded_path,
                    'telegram_date': message.date,
                }
            return {'error': 'Media unavailable on Telegram servers', 'unavailable': True}

        result = client_pool.execute(account, do_download)

        if result and result.get('stalled'):
            # OS thread is back in the pool now. Reset to pending so process_download_queue
            # re-dispatches on the next cycle. Doing this here (not inside the thread) ensures
            # the freed slot and the pending status become visible to the queue processor
            # at the same time, preventing it from dispatching a replacement before re-queuing.
            logger.warning(f"Task {task_id} ({task.original_filename}) stalled, resetting to pending")
            DownloadTask.objects.filter(pk=task_id).update(
                status='pending',
                pending_reason='queued',
                progress=0,
                started_at=None,
            )
            try:
                sync_broadcast_status_change(user_id, task_id, 'pending', task.original_filename)
            except Exception:
                pass
            return

        if result and 'path' in result:
            # Calculate SHA256 and file size in a single pass
            sha256_hash = hashlib.sha256()
            file_size = 0
            with open(result['path'], 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    sha256_hash.update(chunk)
                    file_size += len(chunk)
            file_hash = sha256_hash.hexdigest()

            # Check for duplicates if deduplication enabled
            channel_config = task.channel.config
            is_duplicate = False
            original_file = None

            if channel_config.deduplication_mode == 'sha256':
                existing = DownloadedFile.objects.filter(
                    sha256_hash=file_hash,
                    is_duplicate=False
                ).first()
                if existing:
                    is_duplicate = True
                    original_file = existing
                    file_size = 0
                    # Remove the local file (temp or final), keep only reference
                    os.remove(result['path'])

            # Upload to cloud storage if not a duplicate
            if not is_duplicate and use_cloud:
                try:
                    backend.save_file(relative_path, result['path'])
                    logger.info(f"Uploaded to cloud storage: {relative_path}")
                finally:
                    # Clean up temp file after upload
                    try:
                        if os.path.exists(result['path']):
                            os.remove(result['path'])
                    except Exception:
                        pass

            # Check if task still exists (may have been cancelled during download)
            try:
                task.refresh_from_db()
                task_exists = True
            except DownloadTask.DoesNotExist:
                logger.warning(f"Task {task_id} was deleted during download, saving file without task reference")
                task_exists = False

            # relative_path was computed above (before do_download)
            final_relative_path = relative_path if not is_duplicate else ''

            # Get media dimensions from the ArchivedMessage
            archived_msg = ArchivedMessage.objects.filter(
                channel_id=task.channel_id, message_id=task.message_id
            ).values('media_width', 'media_height', 'media_duration').first()

            stored_filename = Path(relative_path).name if not is_duplicate else ''

            if task_exists:
                # Normal case: task still exists, use get_or_create to handle partial completions
                downloaded_file, created = DownloadedFile.objects.get_or_create(
                    task=task,
                    defaults={
                        'channel': task.channel,
                        'message_id': task.message_id,
                        'original_filename': task.original_filename,
                        'stored_filename': stored_filename,
                        'file_path': final_relative_path,
                        'file_type': task.file_type,
                        'file_size': file_size if not is_duplicate else original_file.file_size,
                        'mime_type': task.mime_type,
                        'sha256_hash': file_hash,
                        'is_duplicate': is_duplicate,
                        'original_file': original_file,
                        'telegram_file_id': task.telegram_file_id,
                        'file_unique_id': task.file_unique_id,
                        'telegram_date': result['telegram_date'],
                        'media_width': archived_msg.get('media_width') if archived_msg else None,
                        'media_height': archived_msg.get('media_height') if archived_msg else None,
                        'media_duration': archived_msg.get('media_duration') if archived_msg else None,
                    }
                )
                if not created:
                    logger.info(f"DownloadedFile already exists for task {task.pk}, skipping creation")
            else:
                # Task was cancelled: create file without task reference
                # Check if file already exists by hash to avoid duplicates
                downloaded_file = DownloadedFile.objects.filter(
                    channel_id=task.channel_id,
                    message_id=task.message_id,
                    sha256_hash=file_hash
                ).first()

                if not downloaded_file:
                    downloaded_file = DownloadedFile.objects.create(
                        task=None,  # No task reference since it was deleted
                        channel_id=task.channel_id,
                        message_id=task.message_id,
                        original_filename=task.original_filename,
                        stored_filename=stored_filename,
                        file_path=final_relative_path,
                        file_type=task.file_type,
                        file_size=file_size if not is_duplicate else original_file.file_size,
                        mime_type=task.mime_type,
                        sha256_hash=file_hash,
                        is_duplicate=is_duplicate,
                        original_file=original_file,
                        telegram_file_id=task.telegram_file_id,
                        file_unique_id=task.file_unique_id,
                        telegram_date=result['telegram_date'],
                        media_width=archived_msg.get('media_width') if archived_msg else None,
                        media_height=archived_msg.get('media_height') if archived_msg else None,
                        media_duration=archived_msg.get('media_duration') if archived_msg else None,
                    )
                    logger.info(f"Created DownloadedFile without task reference for cancelled task {task_id}")

            # Link the ArchivedMessage to this DownloadedFile
            ArchivedMessage.objects.filter(
                channel_id=task.channel_id,
                message_id=task.message_id
            ).update(downloaded_file=downloaded_file)

            # Update task if it still exists
            if task_exists:
                task.status = 'completed'
                task.progress = 100
                task.completed_at = timezone.now()
                task.save()

            # Update channel config progress
            channel_config.downloaded_messages += 1
            channel_config.last_downloaded_message_id = max(
                channel_config.last_downloaded_message_id,
                task.message_id
            )
            channel_config.save()

            logger.info(f"Downloaded: {task.original_filename}")

            # Log download completed
            _log_activity(
                'download_completed',
                f"Downloaded {task.file_type}: {task.original_filename}",
                channel=task.channel,
                filename=task.original_filename,
                file_type=task.file_type,
                file_size=file_size if not is_duplicate else original_file.file_size,
                is_duplicate=is_duplicate,
                task_id=task_id,
            )

            # Broadcast completion via WebSocket
            try:
                sync_broadcast_status_change(
                    user_id, task_id, 'completed', task.original_filename
                )
            except Exception as e:
                logger.debug(f"WebSocket broadcast error: {e}")

            return downloaded_file.pk
        else:
            # Extract specific error message and check if unavailable
            error_msg = 'Failed to download media'
            is_unavailable = False
            if result and isinstance(result, dict):
                error_msg = result.get('error', error_msg)
                is_unavailable = result.get('unavailable', False)

            # Check if task still exists before updating
            try:
                task.refresh_from_db()
                task_exists = True
            except DownloadTask.DoesNotExist:
                logger.warning(f"Task {task_id} was deleted, cannot update status")
                task_exists = False

            if task_exists:
                # Use 'unavailable' status for permanent failures (deleted/no media)
                task.status = 'unavailable' if is_unavailable else 'failed'
                task.last_error = error_msg
                if is_unavailable:
                    task.completed_at = timezone.now()  # Mark as done since nothing more can be done
                task.save()

            # If unavailable, also mark the ArchivedMessage
            if is_unavailable:
                ArchivedMessage.objects.filter(
                    channel_id=task.channel_id,
                    message_id=task.message_id
                ).update(media_unavailable=True)

            # Log download failed
            _log_activity(
                'download_failed',
                f"Failed to download: {task.original_filename}" + (" (unavailable)" if is_unavailable else ""),
                channel=task.channel,
                filename=task.original_filename,
                error=error_msg,
                is_unavailable=is_unavailable,
                task_id=task_id,
            )

            # Broadcast status via WebSocket
            if task_exists:
                try:
                    sync_broadcast_status_change(
                        user_id, task_id, task.status, task.original_filename, error_msg
                    )
                except Exception as e:
                    logger.debug(f"WebSocket broadcast error: {e}")

    except Exception as e:

        # Check if task still exists before updating
        try:
            task.refresh_from_db()
            task_exists = True
        except DownloadTask.DoesNotExist:
            logger.warning(f"Task {task_id} was deleted during download error handling")
            task_exists = False

        # Handle FloodWaitError specially - reset to pending, not failed
        if isinstance(e, FloodWaitError):
            wait_seconds = e.seconds
            logger.warning(f"FloodWaitError for task {task_id}: wait {wait_seconds}s")

            # Log flood wait
            _log_activity(
                'flood_wait',
                f"Flood wait {wait_seconds}s during download",
                channel=task.channel,
                wait_seconds=wait_seconds,
                task_id=task_id,
            )

            # Update account flood wait status
            account.flood_wait_until = timezone.now() + timezone.timedelta(seconds=wait_seconds)
            account.save(update_fields=['flood_wait_until'])

            if task_exists:
                # Reset task to pending with flood wait reason
                task.status = 'pending'
                task.pending_reason = 'account_flood_wait'
                task.progress = 0
                task.last_error = f"Flood wait: {wait_seconds}s"
                task.save()

                # Broadcast status change
                try:
                    sync_broadcast_status_change(
                        user_id, task_id, 'pending', task.original_filename, f"Flood wait: {wait_seconds}s"
                    )
                except Exception as broadcast_err:
                    logger.debug(f"WebSocket broadcast error: {broadcast_err}")

                # Retry after flood wait period using Dramatiq's Retry
                raise Retry(delay=(wait_seconds + 5) * 1000)
            else:
                # Task was cancelled, don't retry
                return

        logger.exception(f"Error downloading task {task_id}")

        if task_exists:
            task.status = 'failed'
            task.last_error = str(e)
            task.retry_count += 1
            task.save()

            # Broadcast failure via WebSocket
            try:
                sync_broadcast_status_change(
                    user_id, task_id, 'failed', task.original_filename, str(e)
                )
            except Exception as broadcast_err:
                logger.debug(f"WebSocket broadcast error: {broadcast_err}")

            if task.can_retry:
                # Let Dramatiq's built-in retry mechanism handle it
                raise Retry(delay=60000)  # Retry after 60 seconds


def _update_pending_reasons():
    """
    Update pending_reason for all pending tasks based on current conditions.
    This ensures the UI shows accurate reasons for why tasks are waiting.
    """
    # Mark tasks for paused channels
    DownloadTask.objects.filter(
        status='pending',
        channel__config__is_paused=True
    ).exclude(
        pending_reason='channel_paused'
    ).update(pending_reason='channel_paused')

    # Clear channel_paused reason for unpaused channels
    DownloadTask.objects.filter(
        status='pending',
        pending_reason='channel_paused',
        channel__config__is_paused=False
    ).update(pending_reason='queued')

    # Mark tasks for accounts in flood wait
    accounts_in_flood_wait = TelegramAccount.objects.filter(
        is_active=True,
        flood_wait_until__gt=timezone.now()
    )
    for account in accounts_in_flood_wait:
        DownloadTask.objects.filter(
            status='pending',
            channel__account=account,
            channel__config__is_paused=False  # Don't override channel_paused
        ).exclude(
            pending_reason='account_flood_wait'
        ).update(pending_reason='account_flood_wait')

    # Clear flood_wait reason for accounts no longer in flood wait
    accounts_not_in_flood_wait = TelegramAccount.objects.filter(
        is_active=True
    ).exclude(
        flood_wait_until__gt=timezone.now()
    )
    for account in accounts_not_in_flood_wait:
        DownloadTask.objects.filter(
            status='pending',
            pending_reason='account_flood_wait',
            channel__account=account
        ).update(pending_reason='queued')

    # Mark tasks as 'no_slots' when all download slots are in use
    # and clear the reason when slots become available
    all_accounts = TelegramAccount.objects.filter(is_active=True, is_authenticated=True)
    for account in all_accounts:
        active_count = DownloadTask.objects.filter(
            channel__account=account,
            status='downloading'
        ).count()
        available_slots = account.max_concurrent_downloads - active_count

        if available_slots <= 0:
            # No slots available - mark pending tasks as 'no_slots'
            # (but don't override higher-priority reasons like channel_paused or flood_wait)
            DownloadTask.objects.filter(
                status='pending',
                channel__account=account,
                pending_reason='queued'  # Only update tasks with no blocking reason
            ).update(pending_reason='no_slots')
        else:
            # Slots available - clear 'no_slots' reason
            DownloadTask.objects.filter(
                status='pending',
                channel__account=account,
                pending_reason='no_slots'
            ).update(pending_reason='queued')

            # Reset 'dispatched' tasks back to 'queued' if nothing is actually downloading
            # This recovers tasks whose RabbitMQ messages were lost or workers restarted
            if active_count == 0:
                dispatched_reset = DownloadTask.objects.filter(
                    status='pending',
                    channel__account=account,
                    pending_reason='dispatched'
                ).update(pending_reason='queued')
                if dispatched_reset > 0:
                    logger.info(f"Reset {dispatched_reset} stuck dispatched tasks for account {account.pk} (0 active downloads)")


@dramatiq.actor(queue_name=QUEUE_DEFAULT)
def process_download_queue():
    """
    Process pending downloads respecting priorities and limits.
    Runs periodically via APScheduler.
    """
    logger.debug("process_download_queue: Starting queue processing")

    # Update pending_reasons for blocked tasks
    _update_pending_reasons()

    # Get all active accounts not in flood wait
    accounts = TelegramAccount.objects.filter(
        is_active=True,
        is_authenticated=True
    )

    logger.debug(f"process_download_queue: Found {accounts.count()} active authenticated accounts")

    for account in accounts:
        if account.is_flood_wait_active:
            logger.debug(f"process_download_queue: Account {account.pk} in flood wait, skipping")
            continue

        # Count active downloads + already-dispatched tasks for this account
        # Both consume slots to prevent over-dispatching during rapid queue runs
        active_count = DownloadTask.objects.filter(
            channel__account=account,
        ).filter(
            Q(status='downloading') | Q(status='pending', pending_reason='dispatched')
        ).count()

        available_slots = account.max_concurrent_downloads - active_count
        logger.debug(
            f"process_download_queue: Account {account.pk} - "
            f"active={active_count}, max={account.max_concurrent_downloads}, available={available_slots}"
        )

        if available_slots <= 0:
            continue

        # Get pending tasks for this account, ordered by priority
        # Use Q objects to handle channels that might not have a config
        pending_tasks = DownloadTask.objects.filter(
            channel__account=account,
            status='pending',
        ).exclude(
            pending_reason='dispatched'  # Already dispatched, waiting for worker pickup
        ).filter(
            Q(channel__config__isnull=True) | Q(channel__config__is_paused=False)
        ).order_by('-priority', 'created_at')[:available_slots]

        logger.debug(f"process_download_queue: Found {len(pending_tasks)} pending tasks for account {account.pk}")

        for task in pending_tasks:
            # Mark as dispatched to prevent duplicate dispatch on next queue run
            task.pending_reason = 'dispatched'
            task.save(update_fields=['pending_reason'])
            download_file.send(task.pk)
            logger.debug(f"Dispatched download task {task.pk}")


@dramatiq.actor(queue_name=QUEUE_DOWNLOADS, max_retries=2, min_backoff=30000, max_backoff=120000)
def download_profile_photo(account_id: int, telegram_user_id: int, user_telegram_id: int, photo_id: int):
    """
    Download a user's profile photo and store as base64.

    This task is queued from the event processor when a new message is received
    from a user with a profile photo. It runs asynchronously to avoid blocking
    the event processing pipeline.

    Args:
        account_id: TelegramAccount primary key
        telegram_user_id: TelegramUser primary key (our DB)
        user_telegram_id: Telegram's user ID
        photo_id: Telegram photo ID to check for changes
    """
    logger.info(f"download_profile_photo: Starting for user {user_telegram_id} (photo_id={photo_id})")

    try:
        account = TelegramAccount.objects.get(pk=account_id)
    except TelegramAccount.DoesNotExist:
        logger.error(f"Account {account_id} not found")
        return {'success': False, 'error': 'Account not found'}

    try:
        telegram_user = TelegramUser.objects.get(pk=telegram_user_id)
    except TelegramUser.DoesNotExist:
        logger.error(f"TelegramUser {telegram_user_id} not found")
        return {'success': False, 'error': 'User not found'}

    # Skip if account not active
    if not account.is_authenticated or not account.is_active:
        logger.warning(f"Account {account_id} not active/authenticated")
        return {'success': False, 'error': 'Account not active'}

    # Skip if we already have this exact photo
    if telegram_user.photo_id == photo_id and telegram_user.profile_photo_base64:
        logger.debug(f"User {user_telegram_id} photo unchanged (id={photo_id})")
        return {'success': False, 'error': 'Photo unchanged'}

    # Check flood wait - reschedule instead of failing
    if account.is_flood_wait_active:
        logger.warning(f"Account is in flood wait, rescheduling")
        download_profile_photo.send_with_options(
            args=(account_id, telegram_user_id, user_telegram_id, photo_id),
            delay=60000
        )
        return {'success': False, 'error': 'Account in flood wait, rescheduled'}

    try:
        async def do_download(client):
            try:
                entity = await client.get_entity(user_telegram_id)
            except ValueError as e:
                # Entity not found - likely a channel ID or unknown entity
                logger.debug(f"Could not resolve entity {user_telegram_id}: {e}")
                return None
            # Only download profile photos for users, not channels
            if not isinstance(entity, User):
                logger.debug(f"Entity {user_telegram_id} is not a User, skipping profile photo")
                return None
            if not entity.photo:
                return None
            photo_bytes = await client.download_profile_photo(entity, file=bytes)
            return photo_bytes

        photo_bytes = client_pool.execute(account, do_download)
        if not photo_bytes:
            logger.debug(f"No photo available for user {user_telegram_id}")
            return {'success': False, 'error': 'No photo available'}

        # Encode to base64 and save
        base64_data = base64.b64encode(photo_bytes).decode('utf-8')
        telegram_user.photo_id = photo_id
        telegram_user.profile_photo_base64 = base64_data
        telegram_user.profile_photo_updated_at = timezone.now()
        telegram_user.save(update_fields=['photo_id', 'profile_photo_base64', 'profile_photo_updated_at'])

        logger.info(f"Downloaded profile photo for user {user_telegram_id} ({len(base64_data)} bytes)")

        _log_activity(
            'photo_downloaded',
            f"Downloaded profile photo for {telegram_user.display_name}",
            source='worker_downloads',
            telegram_user=telegram_user,
        )

        return {'success': True, 'size': len(base64_data)}

    except Exception as e:
        logger.exception(f"Error downloading profile photo for user {user_telegram_id}")
        return {'success': False, 'error': str(e)}


@dramatiq.actor(queue_name=QUEUE_DOWNLOADS, max_retries=2, min_backoff=15000, max_backoff=60000)
def download_thumbnail(
    account_id: int,
    channel_id: int,
    message_id: int,
    media_type: str,
    telegram_file_id: str,
    thumbnail_size: str = 'm',
):
    """
    Download a message thumbnail and update the ArchivedMessage.

    This task is queued from the event processor when a message with media is received
    and the channel has thumbnail downloads enabled.

    Args:
        account_id: TelegramAccount primary key
        channel_id: TelegramChannel primary key
        message_id: Message ID within the channel
        media_type: Type of media (photo, video, file)
        telegram_file_id: Telegram's file ID for the media
        thumbnail_size: Size for photo thumbnails (s=100px, m=320px, x=800px, y=1280px)
    """
    logger.info(f"download_thumbnail: Starting for channel={channel_id} message={message_id}")

    try:
        account = TelegramAccount.objects.get(pk=account_id)
    except TelegramAccount.DoesNotExist:
        logger.error(f"Account {account_id} not found")
        return {'success': False, 'error': 'Account not found'}

    try:
        channel = TelegramChannel.objects.get(pk=channel_id)
    except TelegramChannel.DoesNotExist:
        logger.error(f"Channel {channel_id} not found")
        return {'success': False, 'error': 'Channel not found'}

    # Skip if account not active
    if not account.is_authenticated or not account.is_active:
        logger.warning(f"Account {account_id} not active/authenticated")
        return {'success': False, 'error': 'Account not active'}

    # Check flood wait - reschedule
    if account.is_flood_wait_active:
        logger.warning(f"Account is in flood wait, rescheduling thumbnail download")
        download_thumbnail.send_with_options(
            args=(account_id, channel_id, message_id, media_type, telegram_file_id, thumbnail_size),
            delay=60000
        )
        return {'success': False, 'error': 'Account in flood wait, rescheduled'}

    try:
        global_settings = GlobalSettings.get_settings()
        use_cloud = is_cloud_backend(global_settings)
        backend = get_storage_backend(global_settings)

        thumb_filename = f"thumb_{message_id}.jpg"
        relative_path = f"{channel.telegram_id}/thumbnails/{thumb_filename}"

        # Determine download location
        if use_cloud:
            temp_dir = Path(tempfile.gettempdir()) / 'trawlr-downloads'
            temp_dir.mkdir(parents=True, exist_ok=True)
            thumb_path = temp_dir / thumb_filename
        else:
            thumb_dir = Path(global_settings.storage_root) / str(channel.telegram_id) / 'thumbnails'
            thumb_dir.mkdir(parents=True, exist_ok=True)
            thumb_path = thumb_dir / thumb_filename

        async def do_download(client):
            # Get the message to download thumbnail from
            # Use negative ID format for groups/channels (positive IDs are interpreted as users)
            # Try -100 format first (channels/supergroups), fall back to negative (legacy groups)
            telegram_id = channel.telegram_id
            entity = None
            for peer_id in [int(f"-100{telegram_id}"), -telegram_id, telegram_id]:
                try:
                    entity = await client.get_entity(peer_id)
                    break
                except ValueError:
                    continue

            if not entity:
                logger.error(f"Could not resolve entity for channel {telegram_id}")
                return None

            messages = await client.get_messages(entity, ids=message_id)
            if not messages:
                return None

            message = messages[0] if isinstance(messages, list) else messages
            if not message or not message.media:
                return None

            downloaded = None
            if message.photo:
                # For photos, select the configured size
                sizes = message.photo.sizes
                target_size = None
                fallback_order = {
                    's': ['s', 'm', 'x', 'y', 'w'],
                    'm': ['m', 's', 'x', 'y', 'w'],
                    'x': ['x', 'm', 'y', 's', 'w'],
                    'y': ['y', 'x', 'm', 'w', 's'],
                }
                preferred_types = fallback_order.get(thumbnail_size, ['m', 's', 'x', 'y', 'w'])

                for preferred_type in preferred_types:
                    for size in sizes:
                        if getattr(size, 'type', '') == preferred_type:
                            target_size = size
                            break
                    if target_size:
                        break

                if not target_size and sizes:
                    target_size = sizes[-1]

                downloaded = await client.download_media(
                    message,
                    file=str(thumb_path),
                    thumb=target_size
                )
            else:
                # For videos/documents, use thumb=-1 to get thumbnail
                downloaded = await client.download_media(
                    message,
                    file=str(thumb_path),
                    thumb=-1
                )

            return downloaded

        downloaded = client_pool.execute(account, do_download)

        if not downloaded:
            logger.debug(f"No thumbnail available for message {message_id}")
            return {'success': False, 'error': 'No thumbnail available'}

        # Upload to cloud storage if needed
        if use_cloud:
            try:
                backend.save_file(relative_path, str(thumb_path))
                logger.info(f"Uploaded thumbnail to cloud storage: {relative_path}")
            finally:
                try:
                    if thumb_path.exists():
                        os.remove(thumb_path)
                except Exception:
                    pass

        # Update the ArchivedMessage with the thumbnail path
        updated = ArchivedMessage.objects.filter(
            channel=channel,
            message_id=message_id
        ).update(thumbnail_path=relative_path)

        if updated:
            logger.info(f"Downloaded thumbnail for message {message_id}: {relative_path}")
            return {'success': True, 'path': relative_path}
        else:
            logger.warning(f"ArchivedMessage not found for channel={channel_id} message={message_id}")
            return {'success': False, 'error': 'ArchivedMessage not found'}

    except Exception as e:
        logger.exception(f"Error downloading thumbnail for message {message_id}")
        return {'success': False, 'error': str(e)}


@dramatiq.actor(queue_name=QUEUE_SCANS_HISTORY)
def backfill_thumbnails(channel_id: int, task_run_id: str = None):
    """
    Backfill thumbnails for existing messages that have media but no thumbnail.

    This task queries ArchivedMessage records for the channel that have media
    but no thumbnail_path, then queues download_thumbnail tasks for each.
    """
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

    try:
        channel = TelegramChannel.objects.select_related('account', 'config').get(pk=channel_id)
    except TelegramChannel.DoesNotExist:
        logger.error(f"Channel {channel_id} not found")
        if task_run:
            task_run.mark_failed("Channel not found")
        return

    # Skip inactive sources
    if not channel.active:
        logger.info(f"Channel {channel.title} is inactive, skipping thumbnail backfill")
        if task_run:
            task_run.mark_completed()
        return

    account = channel.account
    config = channel.config
    global_settings = GlobalSettings.get_settings()
    storage_root = global_settings.storage_root

    use_cloud = is_cloud_backend(global_settings)
    backend = get_storage_backend(global_settings)

    _log_activity(
        'thumbnail_backfill_started',
        f"Starting thumbnail backfill for {channel.title}",
        source='worker',
        channel=channel,
    )

    # Find messages with media but no thumbnail
    messages_needing_thumbnails = ArchivedMessage.objects.filter(
        channel=channel,
        has_media=True,
        thumbnail_path='',
    ).exclude(
        media_type=''  # Skip messages where media_type wasn't determined
    ).values_list('message_id', 'media_type').order_by('message_id')

    total_messages = messages_needing_thumbnails.count()
    logger.info(f"Found {total_messages} messages needing thumbnails for {channel.title}")

    if total_messages == 0:
        if task_run:
            task_run.update_progress(
                message="No messages need thumbnails",
                percent=100,
                data={'thumbnails_queued': 0}
            )
            task_run.mark_completed()
        return

    if task_run:
        task_run.update_progress(
            message=f"Found {total_messages} messages needing thumbnails",
            percent=5,
            data={'total': total_messages}
        )

    # Check for flood wait
    if account.is_flood_wait_active:
        logger.warning(f"Account {account.phone_number} is in flood wait, delaying backfill")
        if task_run:
            task_run.update_progress("Waiting for flood wait to clear")
        backfill_thumbnails.send_with_options(
            args=(channel_id,),
            kwargs={'task_run_id': task_run_id},
            delay=60000
        )
        return

    try:
        service = TelegramService(account)

        async def do_backfill():

            def db_sync(func):
                return sync_to_async(func, thread_sensitive=False)

            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number
            )

            try:
                # Get entity
                try:
                    entity = await service._client.get_entity(channel.telegram_id)
                except ValueError:
                    logger.info(f"Entity {channel.telegram_id} not cached, fetching dialogs...")
                    await service._client.get_dialogs(limit=GlobalSettings.get_settings().dialog_cache_limit)
                    entity = await service._client.get_entity(channel.telegram_id)

                thumbnails_downloaded = 0
                thumbnails_failed = 0
                processed = 0
                batch_size = 100
                failed_msg_ids = set()  # Track failed IDs to avoid infinite retries

                # Process in batches to avoid loading all message IDs into memory
                # Always query from offset 0 since processed messages drop out of the queryset
                message_ids = list(messages_needing_thumbnails[:batch_size])

                while message_ids:
                    # Check for cancellation
                    if task_run:
                        if await db_sync(task_run.check_cancelled)():
                            logger.info(f"Thumbnail backfill cancelled for {channel.title}")
                            return {
                                'downloaded': thumbnails_downloaded,
                                'failed': thumbnails_failed,
                                'cancelled': True
                            }

                    for msg_id, media_type in message_ids:
                        processed += 1

                        thumb_filename = f"thumb_{msg_id}.jpg"
                        relative_path = f"{channel.telegram_id}/thumbnails/{thumb_filename}"

                        # Check if thumbnail already exists in storage
                        if use_cloud:
                            already_exists = backend.file_exists(relative_path)
                        else:
                            thumb_check_path = Path(storage_root) / relative_path
                            already_exists = thumb_check_path.exists() and thumb_check_path.stat().st_size > 0

                        if already_exists:
                            # Update DB with existing thumbnail path
                            await db_sync(lambda: ArchivedMessage.objects.filter(
                                channel=channel,
                                message_id=msg_id
                            ).update(thumbnail_path=relative_path))()
                            thumbnails_downloaded += 1
                            continue

                        # For local: clean up empty files from previous failed attempts
                        if not use_cloud:
                            local_thumb = Path(storage_root) / relative_path
                            if local_thumb.exists() and local_thumb.stat().st_size == 0:
                                local_thumb.unlink()
                                logger.debug(f"Deleted empty thumbnail file for message {msg_id}")

                        # Determine download path
                        if use_cloud:
                            dl_dir = Path(tempfile.gettempdir()) / 'trawlr-downloads'
                            dl_dir.mkdir(parents=True, exist_ok=True)
                        else:
                            dl_dir = Path(storage_root) / str(channel.telegram_id) / 'thumbnails'
                            dl_dir.mkdir(parents=True, exist_ok=True)
                        thumb_path = dl_dir / thumb_filename

                        # Download thumbnail
                        try:
                            # Get the message from Telegram
                            messages = await service._client.get_messages(entity, ids=msg_id)
                            if not messages:
                                thumbnails_failed += 1
                                failed_msg_ids.add(msg_id)
                                continue

                            message = messages[0] if isinstance(messages, list) else messages
                            if not message or not message.media:
                                thumbnails_failed += 1
                                failed_msg_ids.add(msg_id)
                                continue

                            # Download thumbnail
                            downloaded = None
                            if message.photo:
                                sizes = message.photo.sizes
                                if sizes:
                                    smallest = min(range(len(sizes)),
                                                   key=lambda i: getattr(sizes[i], 'size', 0) or
                                                                (getattr(sizes[i], 'w', 0) * getattr(sizes[i], 'h', 0)))
                                    downloaded = await service._client.download_media(
                                        message,
                                        file=str(thumb_path),
                                        thumb=smallest
                                    )
                            else:
                                downloaded = await service._client.download_media(
                                    message,
                                    file=str(thumb_path),
                                    thumb=-1
                                )

                            # Verify file was actually downloaded with content
                            if downloaded and thumb_path.exists() and thumb_path.stat().st_size > 0:
                                # Upload to cloud if needed
                                if use_cloud:
                                    try:
                                        backend.save_file(relative_path, str(thumb_path))
                                    finally:
                                        try:
                                            thumb_path.unlink()
                                        except Exception:
                                            pass

                                await db_sync(lambda: ArchivedMessage.objects.filter(
                                    channel=channel,
                                    message_id=msg_id
                                ).update(thumbnail_path=relative_path))()
                                thumbnails_downloaded += 1
                            else:
                                # Clean up empty/failed file
                                if thumb_path.exists():
                                    thumb_path.unlink()
                                thumbnails_failed += 1
                                failed_msg_ids.add(msg_id)

                        except Exception as e:
                            logger.debug(f"Failed to download thumbnail for message {msg_id}: {e}")
                            thumbnails_failed += 1
                            failed_msg_ids.add(msg_id)

                        # Update progress periodically
                        if processed % 10 == 0 and task_run:
                            percent = min(95, int((processed / total_messages) * 100))
                            await db_sync(task_run.update_progress)(
                                message=f"Downloaded {thumbnails_downloaded}/{processed} thumbnails",
                                percent=percent,
                                data={
                                    'downloaded': thumbnails_downloaded,
                                    'failed': thumbnails_failed,
                                    'processed': processed,
                                    'total': total_messages
                                }
                            )

                    # Get next batch - always from offset 0 since processed messages
                    # are excluded by the thumbnail_path='' filter
                    # Also exclude failed message IDs to avoid infinite retry loops
                    queryset = ArchivedMessage.objects.filter(
                        channel=channel,
                        has_media=True,
                        thumbnail_path='',
                    ).exclude(
                        media_type=''
                    )
                    if failed_msg_ids:
                        queryset = queryset.exclude(message_id__in=failed_msg_ids)
                    message_ids = list(
                        queryset.values_list('message_id', 'media_type').order_by('message_id')[:batch_size]
                    )

                return {
                    'downloaded': thumbnails_downloaded,
                    'failed': thumbnails_failed,
                    'total': total_messages,
                }

            finally:
                await service.disconnect()

        result = run_async(do_backfill())

        if task_run:
            if result.get('cancelled'):
                task_run.mark_cancelled()
            else:
                task_run.update_progress(
                    message=f"Downloaded {result['downloaded']} thumbnails ({result['failed']} failed)",
                    percent=100,
                    data=result
                )
                task_run.mark_completed()

        _log_activity(
            'thumbnail_backfill_completed',
            f"Thumbnail backfill completed for {channel.title}: {result['downloaded']} downloaded, {result['failed']} failed",
            source='worker',
            channel=channel,
        )

        logger.info(f"Thumbnail backfill completed for {channel.title}: {result}")

    except Exception as e:
        logger.exception(f"Error in thumbnail backfill for channel {channel_id}")
        if task_run:
            task_run.mark_failed(str(e))
        _log_activity(
            'thumbnail_backfill_failed',
            f"Thumbnail backfill failed for {channel.title}: {e}",
            source='worker',
            channel=channel,
        )


@dramatiq.actor(queue_name=QUEUE_DEFAULT)
def sync_missing_media(channel_id: int, task_run_id: str = None):
    """
    Create download tasks for archived messages that have media but no download.

    This is a DATABASE-ONLY operation - no Telegram API calls.
    Use this to backfill downloads for historical content without causing flood waits.
    """
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

    try:
        channel = TelegramChannel.objects.select_related('account', 'config').get(pk=channel_id)
    except TelegramChannel.DoesNotExist:
        logger.error(f"Channel {channel_id} not found")
        if task_run:
            task_run.mark_failed("Channel not found")
        return

    config = channel.config

    # Check if auto-download is enabled and at least one media type is selected
    if not config.auto_download_enabled:
        logger.info(f"Auto-download not enabled for {channel.title}, skipping sync")
        if task_run:
            task_run.mark_completed()
        return

    if not (config.download_photos or config.download_videos or config.download_files):
        logger.info(f"No media types enabled for {channel.title}, skipping sync")
        if task_run:
            task_run.mark_completed()
        return

    logger.info(f"Starting sync_missing_media for {channel.title}")

    _log_activity(
        'sync_missing_started',
        f"Starting media sync for {channel.title}",
        source='worker',
        channel=channel,
    )

    # Build list of enabled media types
    enabled_types = []
    if config.download_photos:
        enabled_types.append('photo')
    if config.download_videos:
        enabled_types.append('video')
    if config.download_files:
        enabled_types.append('file')

    # Get archived messages with media that match enabled types
    archived_with_media = ArchivedMessage.objects.filter(
        channel=channel,
        has_media=True,
        media_type__in=enabled_types,
        is_deleted=False,
        media_unavailable=False,
    ).exclude(
        telegram_file_id=''
    ).values_list('message_id', 'media_type', 'telegram_file_id', 'original_filename', 'file_size', 'mime_type')

    # Get existing download tasks and downloaded files for this channel
    existing_tasks = set(
        DownloadTask.objects.filter(channel=channel).values_list('message_id', flat=True)
    )
    existing_downloads = set(
        DownloadedFile.objects.filter(channel=channel).values_list('message_id', flat=True)
    )
    already_handled = existing_tasks | existing_downloads

    # Create download tasks for missing media
    tasks_created = 0
    tasks_skipped = 0
    batch_size = 500
    batch = []

    total_messages = archived_with_media.count()

    if task_run:
        task_run.update_progress(
            message=f"Scanning {total_messages} archived messages with media",
            percent=5,
        )

    for idx, (message_id, media_type, file_id, filename, file_size, mime_type) in enumerate(archived_with_media.iterator()):
        if message_id in already_handled:
            tasks_skipped += 1
            continue

        # Calculate priority
        effective_priority = config.priority + config.get_file_type_priority(media_type)

        batch.append(DownloadTask(
            channel=channel,
            message_id=message_id,
            telegram_file_id=file_id,
            original_filename=filename or f"{media_type}_{message_id}",
            file_type=media_type,
            file_size=file_size or 0,
            mime_type=mime_type or '',
            priority=effective_priority,
            max_retries=3,
            pending_reason='queued',
            status='pending',
        ))

        # Bulk create in batches
        if len(batch) >= batch_size:
            DownloadTask.objects.bulk_create(batch, ignore_conflicts=True)
            tasks_created += len(batch)
            batch = []

            if task_run:
                percent = min(95, int(((idx + 1) / total_messages) * 100))
                task_run.update_progress(
                    message=f"Created {tasks_created} download tasks",
                    percent=percent,
                )

    # Create remaining batch
    if batch:
        DownloadTask.objects.bulk_create(batch, ignore_conflicts=True)
        tasks_created += len(batch)

    logger.info(f"sync_missing_media completed for {channel.title}: {tasks_created} tasks created, {tasks_skipped} already existed")

    if task_run:
        task_run.update_progress(
            message=f"Created {tasks_created} download tasks ({tasks_skipped} already existed)",
            percent=100,
            data={'created': tasks_created, 'skipped': tasks_skipped}
        )
        task_run.mark_completed()

    _log_activity(
        'sync_missing_completed',
        f"Media sync completed for {channel.title}: {tasks_created} tasks created",
        source='worker',
        channel=channel,
    )
