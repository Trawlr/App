import asyncio
import logging
from collections import defaultdict

import dramatiq
from asgiref.sync import sync_to_async
from django.db.models import Q
from django.utils import timezone
from telethon.errors import FloodWaitError, ChannelPrivateError
from telethon.tl.functions.messages import GetMessagesReactionsRequest

from accounts.models import GlobalSettings, TelegramAccount
from accounts.telegram_service import TelegramService, run_async
from audit.models import TelegramChannel, TelegramUser, UserReaction
from downloads.models import ArchivedMessage

from .base import logger, QUEUE_DEFAULT, _log_activity

# Max messages to scan per channel per run
MAX_MESSAGES_PER_CHANNEL = 500

# Messages per API call (Telegram supports up to 100)
BATCH_SIZE = 100

# Delay between API calls (seconds)
BATCH_DELAY = 30

# Delay between consecutive channels in a chain (milliseconds)
CHAIN_GAP_MS = 5000


def _dispatch_next_in_chain(remaining):
    """Dispatch the next channel in a per-account chain, if any remain."""
    if not remaining:
        return
    next_acc, next_ch = remaining[0]
    scan_channel_reactions.send_with_options(
        args=(next_acc, next_ch),
        kwargs={'remaining': remaining[1:]},
        delay=CHAIN_GAP_MS,
    )


@dramatiq.actor(queue_name=QUEUE_DEFAULT, max_retries=2, min_backoff=60_000, max_backoff=300_000)
def scan_channel_reactions(account_id: int, channel_id: int, remaining: list = None):
    """
    Scan per-user reactions for recent messages in a channel.

    Channels for a given account are chained sequentially via the ``remaining``
    list so that a single account never has multiple reaction scans in flight —
    this prevents flood-wait pile-ups. After this task finishes (success or
    skip), the first entry in ``remaining`` is dispatched. On flood wait the
    task re-queues itself at the head of the chain so progress is preserved.
    """
    remaining = remaining or []

    # Global kill switch: if reaction scanning is disabled, drop the message
    # (and any chain it's carrying) without dispatching further work. This
    # ensures orphaned/self-rescheduled messages die on first pickup.
    if GlobalSettings.get_settings().reaction_scan_interval == 0:
        logger.info(
            f"scan_channel_reactions: reaction_scan_interval=0, dropping channel "
            f"{channel_id} and {len(remaining)} chained channels"
        )
        return

    logger.info(
        f"scan_channel_reactions: Starting channel {channel_id} on account {account_id} "
        f"({len(remaining)} more queued in chain)"
    )

    try:
        channel = TelegramChannel.objects.select_related('account').get(pk=channel_id)
    except TelegramChannel.DoesNotExist:
        logger.warning(f"Channel {channel_id} not found, skipping")
        _dispatch_next_in_chain(remaining)
        return

    try:
        account = TelegramAccount.objects.get(pk=account_id)
    except TelegramAccount.DoesNotExist:
        logger.warning(f"Account {account_id} not found, skipping")
        _dispatch_next_in_chain(remaining)
        return

    if not account.is_authenticated or not account.is_active:
        logger.warning(f"Account {account_id} not active/authenticated, skipping")
        _dispatch_next_in_chain(remaining)
        return

    if account.is_flood_wait_active:
        flood_remaining = (account.flood_wait_until - timezone.now()).total_seconds()
        delay_ms = int((flood_remaining + 10) * 1000)
        logger.info(
            f"Account {account_id} in flood wait, requeueing chain head in {delay_ms}ms"
        )
        scan_channel_reactions.send_with_options(
            args=(account_id, channel_id),
            kwargs={'remaining': remaining},
            delay=delay_ms,
        )
        return

    # Get messages with reactions
    messages_with_reactions = list(
        ArchivedMessage.objects.filter(
            channel=channel,
        ).exclude(
            reactions={},
        ).order_by('-telegram_date')[:MAX_MESSAGES_PER_CHANNEL].values_list('message_id', flat=True)
    )

    if not messages_with_reactions:
        logger.debug(f"No messages with reactions in channel {channel.title}")
        _dispatch_next_in_chain(remaining)
        return

    logger.info(f"scan_channel_reactions: {len(messages_with_reactions)} messages to scan in {channel.title}")

    # Tracks whether we hit a mid-scan flood wait and already rescheduled the chain head
    hit_flood_wait = False

    try:
        service = TelegramService(account)

        async def do_scan():
            nonlocal hit_flood_wait
            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number,
            )

            total_reactions = 0

            try:
                # Get entity for this channel
                try:
                    entity = await service._client.get_entity(channel.telegram_id)
                except ValueError:
                    await service._client.get_dialogs(limit=GlobalSettings.get_settings().dialog_cache_limit)
                    try:
                        entity = await service._client.get_entity(channel.telegram_id)
                    except ValueError:
                        logger.error(f"Could not find entity for channel {channel.telegram_id}")
                        return 0

                # Process in batches
                batches = [
                    messages_with_reactions[i:i + BATCH_SIZE]
                    for i in range(0, len(messages_with_reactions), BATCH_SIZE)
                ]

                for batch_idx, batch_ids in enumerate(batches):
                    if batch_idx > 0:
                        await asyncio.sleep(BATCH_DELAY)

                    try:
                        updates = await service._client(
                            GetMessagesReactionsRequest(
                                peer=entity,
                                id=batch_ids,
                            )
                        )
                    except FloodWaitError as e:
                        account.flood_wait_until = timezone.now() + timezone.timedelta(seconds=e.seconds)
                        account.listener_status = 'flood_wait'
                        await sync_to_async(account.save, thread_sensitive=False)(
                            update_fields=['flood_wait_until', 'listener_status']
                        )
                        logger.warning(
                            f"FloodWaitError after {batch_idx} batches: {e.seconds}s wait"
                        )
                        hit_flood_wait = True
                        delay_ms = (e.seconds + 10) * 1000
                        scan_channel_reactions.send_with_options(
                            args=(account_id, channel_id),
                            kwargs={'remaining': remaining},
                            delay=delay_ms,
                        )
                        return total_reactions

                    # Parse the updates for per-user reactions
                    batch_reactions = await sync_to_async(
                        _process_reaction_updates, thread_sensitive=False
                    )(updates, channel, account)
                    total_reactions += batch_reactions

                    logger.debug(
                        f"Batch {batch_idx + 1}/{len(batches)}: "
                        f"{batch_reactions} reactions from {len(batch_ids)} messages"
                    )

            finally:
                await service.disconnect()

            return total_reactions

        total = run_async(do_scan())

        logger.info(f"scan_channel_reactions: Completed for {channel.title} — {total} reactions stored")

        # Only log completion for a full pass; a mid-scan flood wait will retry this channel
        if total > 0 and not hit_flood_wait:
            _log_activity(
                'reaction_scan_completed',
                f"Scanned reactions in {channel.title}: {total} per-user reactions stored",
                source='worker_telegram',
                channel=channel,
            )

    except ChannelPrivateError:
        logger.warning(f"Channel {channel.title} is private/inaccessible, skipping")
    except Exception as e:
        logger.exception(f"scan_channel_reactions: Failed for channel {channel_id}: {e}")

    # Advance the chain only if we didn't re-queue this same channel for a flood wait
    if not hit_flood_wait:
        _dispatch_next_in_chain(remaining)


@dramatiq.actor(queue_name=QUEUE_DEFAULT, max_retries=1)
def scan_all_channel_reactions():
    """
    Start a per-account serial chain of reaction scans.

    Within a chain, the next channel scan only starts once the previous one
    completes — this keeps API usage predictable and prevents the flood-wait
    pile-up that happens when many channels on the same account scan
    concurrently.
    """
    settings = GlobalSettings.get_settings()
    if settings.reaction_scan_interval == 0:
        logger.debug("Reaction scanning is disabled")
        return

    channels = TelegramChannel.objects.filter(
        active=True,
        account__is_active=True,
        account__is_authenticated=True,
    ).select_related('account')

    chains = defaultdict(list)
    for channel in channels:
        has_reactions = ArchivedMessage.objects.filter(
            channel=channel,
        ).exclude(reactions={}).exists()
        if has_reactions:
            chains[channel.account_id].append([channel.account_id, channel.pk])

    total_channels = sum(len(c) for c in chains.values())
    if not total_channels:
        logger.info("scan_all_channel_reactions: no channels with reactions to scan")
        return

    for account_id, chain in chains.items():
        first_acc, first_ch = chain[0]
        scan_channel_reactions.send_with_options(
            args=(first_acc, first_ch),
            kwargs={'remaining': chain[1:]},
        )

    logger.info(
        f"scan_all_channel_reactions: started {len(chains)} account chain(s) "
        f"covering {total_channels} channel(s)"
    )


def _process_reaction_updates(updates, channel, account):
    """
    Parse reaction updates and upsert UserReaction records.
    Called synchronously (wrapped in sync_to_async by caller).
    """
    if not updates or not hasattr(updates, 'updates'):
        return 0

    total_created = 0

    for update in updates.updates:
        # UpdateMessageReactions contains the per-user reaction list
        if not hasattr(update, 'reactions') or not update.reactions:
            continue

        msg_id = getattr(update, 'msg_id', None)
        if not msg_id:
            continue

        # Find the archived message
        archived_msg = ArchivedMessage.objects.filter(
            channel=channel,
            message_id=msg_id,
        ).first()
        if not archived_msg:
            continue

        reactions = update.reactions
        if not hasattr(reactions, 'recent_reactions') or not reactions.recent_reactions:
            continue

        # Also update the aggregate reactions on the message
        aggregate = {}
        if hasattr(reactions, 'results') and reactions.results:
            for rc in reactions.results:
                reaction = rc.reaction
                if hasattr(reaction, 'emoticon'):
                    aggregate[reaction.emoticon] = rc.count or 0
                elif hasattr(reaction, 'document_id'):
                    aggregate[f"custom:{reaction.document_id}"] = rc.count or 0
            if aggregate:
                archived_msg.reactions = aggregate
                archived_msg.save(update_fields=['reactions'])

        # Process per-user reactions
        for peer_reaction in reactions.recent_reactions:
            # Extract user ID from peer
            peer = peer_reaction.peer_id
            user_id = getattr(peer, 'user_id', None)
            if not user_id:
                continue

            # Extract emoji
            reaction = peer_reaction.reaction
            if hasattr(reaction, 'emoticon'):
                emoji = reaction.emoticon
                is_custom = False
                custom_id = None
            elif hasattr(reaction, 'document_id'):
                emoji = f"custom:{reaction.document_id}"
                is_custom = True
                custom_id = reaction.document_id
            else:
                continue

            reacted_at = getattr(peer_reaction, 'date', None)

            # Try to link to tracked user
            tracked_user = TelegramUser.objects.filter(telegram_id=user_id).first()

            # Upsert
            UserReaction.objects.update_or_create(
                message=archived_msg,
                telegram_user_id=user_id,
                emoji=emoji,
                defaults={
                    'channel': channel,
                    'user': tracked_user,
                    'is_custom_emoji': is_custom,
                    'custom_emoji_id': custom_id,
                    'reacted_at': reacted_at,
                },
            )
            total_created += 1

    return total_created
