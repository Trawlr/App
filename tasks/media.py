"""
Media management tasks (deletion, cleanup).
"""

import dramatiq

from accounts.models import GlobalSettings
from audit.models import ActivityLog, TelegramUser
from downloads.models import ArchivedMessage, DownloadedFile, TaskRun
from storage.utils import get_storage_backend

from .base import logger, QUEUE_DEFAULT


@dramatiq.actor(queue_name=QUEUE_DEFAULT, priority=0, max_retries=3, min_backoff=5000)
def delete_user_media(telegram_user_id: int, task_run_id: str = None):
    """
    Delete all media files and thumbnails for posts by a specific user.
    Runs as a background task to avoid request timeouts.
    """
    logger.info(f"delete_user_media: Starting for user {telegram_user_id}")

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
        telegram_user = TelegramUser.objects.get(pk=telegram_user_id)
    except TelegramUser.DoesNotExist:
        logger.error(f"TelegramUser {telegram_user_id} not found")
        if task_run:
            task_run.mark_failed("User not found")
        return

    backend = get_storage_backend()

    # Log start activity
    ActivityLog.log(
        'user_media_delete_started',
        f'Started deleting all media for {telegram_user.display_name}',
        source='worker',
        telegram_user=telegram_user,
    )

    # Get all archived messages and downloaded files by this user
    user_messages = ArchivedMessage.objects.filter(
        sender_id=telegram_user.telegram_id,
    )
    downloaded_files = DownloadedFile.objects.filter(
        archived_messages__sender_id=telegram_user.telegram_id,
    ).distinct().select_related('original_file')

    files_deleted = 0
    thumbnails_deleted = 0
    errors = 0
    total_processed = 0

    logger.info(
        f"delete_user_media: Found {downloaded_files.count()} downloaded files "
        f"for telegram_id={telegram_user.telegram_id}"
    )

    total_files = downloaded_files.count()
    total_thumbs = user_messages.filter(thumbnail_path__gt='').count()
    total_items = total_files + total_thumbs

    if task_run:
        task_run.update_progress(
            message=f"Processing {total_files} files and {total_thumbs} thumbnails",
            percent=0,
            data={'total_files': total_files, 'total_thumbs': total_thumbs},
        )

    for df in downloaded_files.iterator():
        # Collect all physical file paths to delete (own + original if duplicate)
        paths_to_delete = set()
        if df.file_path:
            paths_to_delete.add(df.file_path)
        if df.is_duplicate and df.original_file and df.original_file.file_path:
            paths_to_delete.add(df.original_file.file_path)
            # Mark the original as deleted too
            if not df.original_file.deleted_from_disk:
                df.original_file.deleted_from_disk = True
                df.original_file.save(update_fields=['deleted_from_disk'])

        for file_path in paths_to_delete:
            try:
                backend.delete_file(file_path)
                files_deleted += 1
            except Exception as e:
                logger.debug(f"Error deleting file {file_path}: {e}")
                errors += 1

        # Delete downloaded file thumbnail
        if df.thumbnail_path:
            try:
                backend.delete_file(df.thumbnail_path)
                thumbnails_deleted += 1
                df.thumbnail_path = ''
            except Exception:
                errors += 1

        df.deleted_from_disk = True
        df.save(update_fields=['deleted_from_disk', 'thumbnail_path'])

        total_processed += 1
        if task_run and total_processed % 50 == 0:
            task_run.update_progress(
                message=f"Processed {total_processed}/{total_items}",
                percent=int((total_processed / total_items) * 100) if total_items else 100,
            )

    # Delete archived message thumbnails
    for msg in user_messages.filter(thumbnail_path__gt='').iterator():
        try:
            backend.delete_file(msg.thumbnail_path)
            thumbnails_deleted += 1
            msg.thumbnail_path = ''
            msg.save(update_fields=['thumbnail_path'])
        except Exception as e:
            logger.debug(f"Error deleting thumbnail for message {msg.pk}: {e}")
            errors += 1

        total_processed += 1
        if task_run and total_processed % 50 == 0:
            task_run.update_progress(
                message=f"Processed {total_processed}/{total_items}",
                percent=int((total_processed / total_items) * 100) if total_items else 100,
            )

    result_msg = (
        f'Deleted {files_deleted} files and {thumbnails_deleted} thumbnails '
        f'for {telegram_user.display_name}'
    )
    if errors:
        result_msg += f' ({errors} errors)'

    logger.info(f"delete_user_media: {result_msg}")

    # Log completion activity
    ActivityLog.log(
        'user_media_delete_completed',
        result_msg,
        source='worker',
        telegram_user=telegram_user,
        files_deleted=files_deleted,
        thumbnails_deleted=thumbnails_deleted,
        errors=errors,
    )

    if task_run:
        task_run.update_progress(
            message=result_msg,
            percent=100,
            data={
                'files_deleted': files_deleted,
                'thumbnails_deleted': thumbnails_deleted,
                'errors': errors,
            },
        )
        task_run.mark_completed()
