"""
Dramatiq tasks for the Trawlr application.

This package organizes background tasks by domain for better maintainability.
All tasks are re-exported here for backwards compatibility.
"""

# Base utilities and constants
from .base import (
    logger,
    QUEUE_DEFAULT,
    QUEUE_DOWNLOADS,
    QUEUE_SCANS_HISTORY,
    QUEUE_SCANS_MEMBERS,
    STUCK_TASK_TIMEOUT_HOURS,
    _log_activity,
    dispatch_task,
)

# Maintenance tasks
from .maintenance import (
    recover_stuck_tasks,
    cleanup_completed_tasks,
    get_dead_letter_stats,
    requeue_dead_letters,
    purge_dead_letters,
)

# Download tasks
from .downloads import (
    download_file,
    process_download_queue,
    process_profile_photo_queue,
    download_profile_photo,
    download_thumbnail,
    backfill_thumbnails,
    sync_missing_media,
)

# Scanning tasks
from .scanning import (
    scan_channel_history,
    scan_all_channel_history,
    scan_channel_members,
    scan_all_channel_members_for_user,
)

# Availability checking tasks
from .availability import (
    check_source_availability,
    check_account_availability,
    check_all_source_availability,
)

# Sync tasks
from .sync import (
    sync_account_channels,
    sync_all_account_channels,
    sync_forum_topics,
    sync_all_forum_topics,
)

# Stats tasks
from .stats import (
    fetch_media_counts,
    refresh_all_media_counts,
    refresh_all_channel_stats,
    refresh_channel_stats,
)

# Profile tasks
from .profiles import (
    fetch_user_profiles,
    fetch_single_user_profile,
)

# Media management tasks
from .media import (
    delete_user_media,
)

# Onboarding tasks
from .onboarding import (
    run_channel_onboarding,
)

# Invite link resolution tasks
from .invite_links import (
    resolve_pending_invite_links,
)

# Reaction scan tasks
from .reactions import (
    scan_channel_reactions,
    scan_all_channel_reactions,
)

# All public exports for `from tasks import *`
__all__ = [
    # Constants
    'QUEUE_DEFAULT',
    'QUEUE_DOWNLOADS',
    'QUEUE_SCANS_HISTORY',
    'QUEUE_SCANS_MEMBERS',
    'STUCK_TASK_TIMEOUT_HOURS',
    # Utilities
    'logger',
    '_log_activity',
    'dispatch_task',
    # Maintenance
    'recover_stuck_tasks',
    'cleanup_completed_tasks',
    'get_dead_letter_stats',
    'requeue_dead_letters',
    'purge_dead_letters',
    # Downloads
    'download_file',
    'process_download_queue',
    'process_profile_photo_queue',
    'download_profile_photo',
    'download_thumbnail',
    'backfill_thumbnails',
    'sync_missing_media',
    # Scanning
    'scan_channel_history',
    'scan_all_channel_history',
    'scan_channel_members',
    'scan_all_channel_members_for_user',
    # Availability
    'check_source_availability',
    'check_account_availability',
    'check_all_source_availability',
    # Sync
    'sync_account_channels',
    'sync_all_account_channels',
    'sync_forum_topics',
    'sync_all_forum_topics',
    # Stats
    'fetch_media_counts',
    'refresh_all_media_counts',
    'refresh_all_channel_stats',
    'refresh_channel_stats',
    # Profiles
    'fetch_user_profiles',
    'fetch_single_user_profile',
    # Media management
    'delete_user_media',
    # Onboarding
    'run_channel_onboarding',
    # Invite link resolution
    'resolve_pending_invite_links',
    # Reaction scanning
    'scan_channel_reactions',
    'scan_all_channel_reactions',
]
