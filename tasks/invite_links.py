import asyncio
import logging

import dramatiq
from django.utils import timezone
from telethon.errors import FloodWaitError, InviteHashExpiredError, InviteHashInvalidError
from telethon.tl.functions.messages import CheckChatInviteRequest

from accounts.models import GlobalSettings, TelegramAccount
from accounts.telegram_service import TelegramService, run_async
from audit.models import TelegramChannel, TgLink, TgLinkEvent

from .base import logger, QUEUE_DEFAULT, _log_activity

# Delay between API calls within a single batch (seconds)
BATCH_RESOLVE_DELAY = 30

# Max unique hashes to resolve per batch run
BATCH_SIZE = 20


@dramatiq.actor(queue_name=QUEUE_DEFAULT, max_retries=3, min_backoff=60_000, max_backoff=300_000)
def resolve_pending_invite_links(account_id: int):
    """
    Resolve pending TgLinkEvents using a single Telegram connection.
    Opens one client session, processes up to BATCH_SIZE unique hashes with
    BATCH_RESOLVE_DELAY seconds between calls, then disconnects.
    Re-dispatches itself if more pending events remain.
    """
    logger.info(f"resolve_pending_invite_links: Starting for account {account_id}")

    # Check global setting
    settings = GlobalSettings.get_settings()
    if not settings.tglink_resolution:
        logger.debug("TG link resolution disabled globally, skipping")
        return

    # Get account
    try:
        account = TelegramAccount.objects.get(pk=account_id)
    except TelegramAccount.DoesNotExist:
        logger.warning(f"Account {account_id} not found, skipping")
        return

    if not account.is_authenticated or not account.is_active:
        logger.warning(f"Account {account_id} not active/authenticated, skipping")
        return

    if account.is_flood_wait_active:
        flood_remaining = (account.flood_wait_until - timezone.now()).total_seconds()
        delay_ms = int((flood_remaining + 10) * 1000)
        logger.info(f"Account {account_id} in flood wait, retrying in {delay_ms}ms")
        resolve_pending_invite_links.send_with_options(
            args=(account_id,),
            delay=delay_ms,
        )
        return

    # Get all pending events for channels owned by this account
    pending_events = list(
        TgLinkEvent.objects.filter(
            resolution_status='pending',
            channel__account_id=account_id,
        ).select_related('channel').order_by('detected_at')
    )

    if not pending_events:
        logger.debug(f"No pending invite link events for account {account_id}")
        return

    # Deduplicate by invite_hash — only resolve each unique hash once
    unique_hashes = {}
    for event in pending_events:
        if event.invite_hash not in unique_hashes:
            unique_hashes[event.invite_hash] = []
        unique_hashes[event.invite_hash].append(event)

    # Cap at BATCH_SIZE
    hashes_to_resolve = dict(list(unique_hashes.items())[:BATCH_SIZE])
    remaining = len(unique_hashes) - len(hashes_to_resolve)

    logger.info(
        f"resolve_pending_invite_links: {len(pending_events)} pending events, "
        f"{len(hashes_to_resolve)} unique hashes this batch "
        f"({remaining} remaining) for account {account_id}"
    )

    try:
        service = TelegramService(account)

        async def do_batch_resolve():
            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number,
            )

            resolved_count = 0
            failed_count = 0

            try:
                for idx, (invite_hash, events) in enumerate(hashes_to_resolve.items()):
                    # Delay between calls (skip delay before first call)
                    if idx > 0:
                        await asyncio.sleep(BATCH_RESOLVE_DELAY)

                    try:
                        result = await service._client(CheckChatInviteRequest(invite_hash))
                        _process_resolved(result, invite_hash, events, account)
                        resolved_count += 1

                    except InviteHashExpiredError:
                        _handle_link_status_batch(events, account, invite_hash, 'expired', 'Invite link has expired')
                        resolved_count += 1

                    except InviteHashInvalidError:
                        _handle_link_status_batch(events, account, invite_hash, 'invalid', 'Invalid invite link')
                        resolved_count += 1

                    except FloodWaitError as e:
                        # Stop batch immediately, update account, reschedule
                        account.flood_wait_until = timezone.now() + timezone.timedelta(seconds=e.seconds)
                        account.listener_status = 'flood_wait'
                        account.save(update_fields=['flood_wait_until', 'listener_status'])

                        logger.warning(
                            f"FloodWaitError after {resolved_count} resolutions: "
                            f"{e.seconds}s wait, rescheduling remaining"
                        )
                        delay_ms = (e.seconds + 10) * 1000
                        resolve_pending_invite_links.send_with_options(
                            args=(account_id,),
                            delay=delay_ms,
                        )
                        return resolved_count, failed_count

                    except Exception as e:
                        logger.exception(f"Failed to resolve {invite_hash[:12]}: {e}")
                        for event in events:
                            event.resolution_status = 'failed'
                            event.resolution_error = str(e)[:500]
                            event.save(update_fields=['resolution_status', 'resolution_error'])
                        failed_count += 1

            finally:
                await service.disconnect()

            return resolved_count, failed_count

        resolved, failed = run_async(do_batch_resolve())

        logger.info(
            f"resolve_pending_invite_links: Completed for account {account_id} — "
            f"{resolved} resolved, {failed} failed"
        )

        # If there are more pending, reschedule
        if remaining > 0:
            logger.info(f"Rescheduling for {remaining} remaining hashes")
            resolve_pending_invite_links.send_with_options(
                args=(account_id,),
                delay=BATCH_RESOLVE_DELAY * 1000,
            )

    except Exception as e:
        logger.exception(f"resolve_pending_invite_links: Batch failed for account {account_id}: {e}")


def _process_resolved(invite_info, invite_hash, events, account):
    """Process a successful CheckChatInviteRequest response."""
    tg_link_data = {
        'last_checked': timezone.now(),
        'status': 'active',
    }

    telegram_id = None

    if hasattr(invite_info, 'chat'):
        # ChatInviteAlready — we're a member, full info available
        chat = invite_info.chat
        telegram_id = chat.id
        tg_link_data.update({
            'telegram_id': telegram_id,
            'title': getattr(chat, 'title', ''),
            'username': getattr(chat, 'username', '') or '',
            'member_count': getattr(chat, 'participants_count', None),
            'is_megagroup': getattr(chat, 'megagroup', False),
            'is_broadcast': getattr(chat, 'broadcast', False),
            'is_verified': getattr(chat, 'verified', False),
            'is_scam': getattr(chat, 'scam', False),
            'is_fake': getattr(chat, 'fake', False),
        })
    else:
        # ChatInvite — preview only
        tg_link_data.update({
            'title': getattr(invite_info, 'title', ''),
            'about': getattr(invite_info, 'about', '') or '',
            'member_count': getattr(invite_info, 'participants_count', None),
            'is_megagroup': getattr(invite_info, 'megagroup', False),
            'is_broadcast': getattr(invite_info, 'broadcast', False),
            'is_request_needed': getattr(invite_info, 'request_needed', False),
        })

    # Upsert TgLink: match on telegram_id if known
    tg_link = None
    if telegram_id:
        tg_link = TgLink.objects.filter(telegram_id=telegram_id).first()

    if tg_link:
        for key, value in tg_link_data.items():
            setattr(tg_link, key, value)
        tg_link.save()
    else:
        tg_link = TgLink.objects.create(**tg_link_data)

    # Try to link to existing tracked channel
    if telegram_id and not tg_link.channel:
        tracked_channel = TelegramChannel.objects.filter(telegram_id=telegram_id).first()
        if tracked_channel:
            tg_link.channel = tracked_channel
            tg_link.save(update_fields=['channel'])

    # Update all events with this hash
    now = timezone.now()
    for event in events:
        event.source_link = tg_link
        event.resolved_at = now
        event.resolved_by = account
        event.resolution_status = 'resolved'
        event.resolution_error = ''
        event.save(update_fields=[
            'source_link', 'resolved_at', 'resolved_by',
            'resolution_status', 'resolution_error',
        ])

    # Also resolve any other pending events with the same hash (from other messages)
    TgLinkEvent.objects.filter(
        invite_hash=invite_hash,
        resolution_status='pending',
    ).update(
        source_link=tg_link,
        resolved_at=now,
        resolution_status='resolved',
        resolved_by=account,
    )

    logger.info(f"Resolved {invite_hash[:12]} -> '{tg_link.title}' (id={tg_link.pk})")

    _log_activity(
        'tglink_resolved',
        f"Resolved invite link to '{tg_link.title}' "
        f"({tg_link.member_count or '?'} members)",
        source='worker_telegram',
        channel=events[0].channel,
    )


def _handle_link_status_batch(events, account, invite_hash, status, error_msg):
    """Handle expired/invalid invite links for a batch of events."""
    logger.info(f"resolve_invite_link: {status} - {invite_hash[:12]}")

    # Try to find an existing TgLink from other events with same hash
    existing_event = TgLinkEvent.objects.filter(
        invite_hash=invite_hash,
        source_link__isnull=False,
    ).exclude(pk__in=[e.pk for e in events]).first()

    if existing_event:
        tg_link = existing_event.source_link
    else:
        tg_link = TgLink.objects.create(
            title=f"Unknown ({status})",
            status=status,
            last_checked=timezone.now(),
        )

    tg_link.status = status
    tg_link.last_checked = timezone.now()
    tg_link.save(update_fields=['status', 'last_checked'])

    now = timezone.now()
    for event in events:
        event.source_link = tg_link
        event.resolved_at = now
        event.resolved_by = account
        event.resolution_status = status
        event.resolution_error = error_msg
        event.save(update_fields=[
            'source_link', 'resolved_at', 'resolved_by',
            'resolution_status', 'resolution_error',
        ])

    # Update any other pending events with the same hash
    TgLinkEvent.objects.filter(
        invite_hash=invite_hash,
        resolution_status='pending',
    ).update(
        source_link=tg_link,
        resolved_at=now,
        resolution_status=status,
        resolved_by=account,
    )
