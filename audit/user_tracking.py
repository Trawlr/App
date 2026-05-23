"""
User tracking utilities for identifying and tracking Telegram users.
"""

import logging
import base64
from django.utils import timezone
from .models import TelegramUser, TelegramUserAlias, UserGroupMembership
from django.db import transaction
from django.db.models import F
from asgiref.sync import sync_to_async
from accounts.telegram_service import TelegramService, run_async
from telethon.errors import FloodWaitError

logger = logging.getLogger('trawlr.audit')


def track_user_from_message(sender, channel, message_date=None):
    """
    Track a user from a message sender object.
    Creates or updates TelegramUser and UserGroupMembership records.

    Args:
        sender: Telethon User object (message.sender)
        channel: TelegramChannel model instance
        message_date: Optional datetime of the message

    Returns:
        tuple: (TelegramUser instance, bool created)
    """
    
    if sender is None:
        return None, False

    sender_id = getattr(sender, 'id', None)
    if not sender_id:
        return None, False

    # Get or create the user
    user, created = TelegramUser.objects.get_or_create(
        telegram_id=sender_id,
        defaults={
            'first_name': getattr(sender, 'first_name', '') or '',
            'last_name': getattr(sender, 'last_name', '') or '',
            'username': getattr(sender, 'username', '') or '',
            'phone': getattr(sender, 'phone', '') or '',
            'is_bot': getattr(sender, 'bot', False) or False,
            'is_verified': getattr(sender, 'verified', False) or False,
            'is_premium': getattr(sender, 'premium', False) or False,
            'is_scam': getattr(sender, 'scam', False) or False,
            'is_fake': getattr(sender, 'fake', False) or False,
            'is_restricted': getattr(sender, 'restricted', False) or False,
        }
    )

    if not created:
        # Update existing user if any fields changed
        user.update_from_telethon(sender)
        user.message_count += 1
        user.save()
    else:
        user.message_count = 1
        user.save()

    # Track membership in this channel
    membership, mem_created = UserGroupMembership.objects.get_or_create(
        user=user,
        channel=channel,
        defaults={
            'last_message_date': message_date or timezone.now(),
        }
    )

    if not mem_created:
        membership.last_seen = timezone.now()
        membership.message_count += 1
        if message_date:
            membership.last_message_date = message_date
        membership.save()

    return user, created


def track_user_from_participant(participant, channel, is_admin=False, is_creator=False, admin_title=''):
    """
    Track a user from a channel participant object.
    Creates or updates TelegramUser and UserGroupMembership records.

    Args:
        participant: Telethon ChannelParticipant or User object
        channel: TelegramChannel model instance
        is_admin: Whether user is an admin
        is_creator: Whether user is the creator
        admin_title: Custom admin title if any

    Returns:
        tuple: (TelegramUser instance, bool created)
    """
    # Get the user object from participant
    user_obj = participant
    if hasattr(participant, 'user'):
        user_obj = participant.user

    if user_obj is None:
        return None, False

    user_id = getattr(user_obj, 'id', None)
    if not user_id:
        return None, False

    # Get or create the user
    user, created = TelegramUser.objects.get_or_create(
        telegram_id=user_id,
        defaults={
            'first_name': getattr(user_obj, 'first_name', '') or '',
            'last_name': getattr(user_obj, 'last_name', '') or '',
            'username': getattr(user_obj, 'username', '') or '',
            'phone': getattr(user_obj, 'phone', '') or '',
            'is_bot': getattr(user_obj, 'bot', False) or False,
            'is_verified': getattr(user_obj, 'verified', False) or False,
            'is_premium': getattr(user_obj, 'premium', False) or False,
            'is_scam': getattr(user_obj, 'scam', False) or False,
            'is_fake': getattr(user_obj, 'fake', False) or False,
            'is_restricted': getattr(user_obj, 'restricted', False) or False,
        }
    )

    if not created:
        user.update_from_telethon(user_obj)
        user.save()

    # Track membership
    membership, mem_created = UserGroupMembership.objects.get_or_create(
        user=user,
        channel=channel,
        defaults={
            'is_admin': is_admin,
            'is_creator': is_creator,
            'admin_title': admin_title,
        }
    )

    if not mem_created:
        membership.last_seen = timezone.now()
        membership.is_admin = is_admin
        membership.is_creator = is_creator
        membership.admin_title = admin_title
        membership.save()

    return user, created


def _extract_user_data(user_obj):
    """
    Extract user data from a Telethon User object into a plain Python dict.
    This must be called from the async context BEFORE passing to sync_to_async.
    """
    if user_obj is None:
        return None

    user_id = getattr(user_obj, 'id', None)
    if not user_id:
        return None

    # Extract photo_id if available
    photo_id = None
    if hasattr(user_obj, 'photo') and user_obj.photo:
        photo_id = getattr(user_obj.photo, 'photo_id', None)

    return {
        'telegram_id': user_id,
        'first_name': getattr(user_obj, 'first_name', '') or '',
        'last_name': getattr(user_obj, 'last_name', '') or '',
        'username': getattr(user_obj, 'username', '') or '',
        'phone': getattr(user_obj, 'phone', '') or '',
        'is_bot': getattr(user_obj, 'bot', False) or False,
        'is_verified': getattr(user_obj, 'verified', False) or False,
        'is_premium': getattr(user_obj, 'premium', False) or False,
        'is_scam': getattr(user_obj, 'scam', False) or False,
        'is_fake': getattr(user_obj, 'fake', False) or False,
        'is_restricted': getattr(user_obj, 'restricted', False) or False,
        'is_deleted': getattr(user_obj, 'deleted', False) or False,
        'is_support': getattr(user_obj, 'support', False) or False,
        'is_contact': getattr(user_obj, 'contact', False) or False,
        'is_mutual_contact': getattr(user_obj, 'mutual_contact', False) or False,
        'is_close_friend': getattr(user_obj, 'close_friend', False) or False,
        'lang_code': getattr(user_obj, 'lang_code', '') or '',
        'stories_hidden': getattr(user_obj, 'stories_hidden', False) or False,
        'stories_unavailable': getattr(user_obj, 'stories_unavailable', False) or False,
        'access_hash': getattr(user_obj, 'access_hash', None),
        'photo_id': photo_id,
    }


def _record_user_alias(user, user_data, channel=None, message_id=None):
    """
    Record the observed (first_name, last_name, username) triple as an alias for
    the user. Append-only: distinct triples create new rows, repeat sightings
    bump last_seen and observation_count.

    Called on every sighting so we capture the name in use at the time — even
    when we don't detect the underlying Telegram profile change event.
    """
    now = timezone.now()
    alias, created = TelegramUserAlias.objects.get_or_create(
        user=user,
        first_name=user_data.get('first_name') or '',
        last_name=user_data.get('last_name') or '',
        username=user_data.get('username') or '',
        defaults={
            'first_seen': now,
            'last_seen': now,
            'observation_count': 1,
            'first_seen_channel': channel,
            'first_seen_message_id': message_id,
        },
    )
    if not created:
        TelegramUserAlias.objects.filter(pk=alias.pk).update(
            last_seen=now,
            observation_count=F('observation_count') + 1,
        )
    return alias, created


def _track_user_from_data(user_data, channel, message_date=None, message_id=None, is_admin=False, is_creator=False, admin_title=''):
    """
    Track a user from pre-extracted user data dict.
    This is the sync function that does the actual DB operations.

    Optimized to minimize database round-trips using:
    - update_or_create for atomic upserts
    - F() expressions for atomic counter increments
    - Batched updates with update_fields
    """
    if user_data is None:
        return None, False

    with transaction.atomic():
        defaults = {
            'first_name': user_data['first_name'],
            'last_name': user_data['last_name'],
            'username': user_data['username'],
            'phone': user_data['phone'],
            'is_bot': user_data['is_bot'],
            'is_verified': user_data['is_verified'],
            'is_premium': user_data['is_premium'],
            'is_scam': user_data['is_scam'],
            'is_fake': user_data['is_fake'],
            'is_restricted': user_data['is_restricted'],
            'is_deleted': user_data.get('is_deleted', False),
            'is_support': user_data.get('is_support', False),
            'is_contact': user_data.get('is_contact', False),
            'is_mutual_contact': user_data.get('is_mutual_contact', False),
            'is_close_friend': user_data.get('is_close_friend', False),
            'lang_code': user_data.get('lang_code', ''),
            'stories_hidden': user_data.get('stories_hidden', False),
            'stories_unavailable': user_data.get('stories_unavailable', False),
            'access_hash': user_data.get('access_hash'),
            'photo_id': user_data.get('photo_id'),
        }

        # Try to get existing user first
        user = TelegramUser.objects.filter(telegram_id=user_data['telegram_id']).first()

        if user is None:
            # Create new user
            user = TelegramUser.objects.create(telegram_id=user_data['telegram_id'], **defaults)
            created = True
        else:
            # Update existing user - build update dict for changed fields only
            created = False
            update_fields = ['last_seen']
            user.last_seen = timezone.now()

            for field in ['first_name', 'last_name', 'username', 'phone', 'is_bot',
                          'is_verified', 'is_premium', 'is_scam', 'is_fake', 'is_restricted',
                          'is_deleted', 'is_support', 'is_contact', 'is_mutual_contact',
                          'is_close_friend', 'lang_code', 'stories_hidden', 'stories_unavailable']:
                new_value = user_data.get(field)
                if new_value is not None and getattr(user, field) != new_value:
                    setattr(user, field, new_value)
                    update_fields.append(field)

            # Update access_hash if provided and different
            if user_data.get('access_hash') and user.access_hash != user_data['access_hash']:
                user.access_hash = user_data['access_hash']
                update_fields.append('access_hash')

            # Update photo_id if provided and different
            if user_data.get('photo_id') and user.photo_id != user_data['photo_id']:
                user.photo_id = user_data['photo_id']
                update_fields.append('photo_id')

            # Increment message count atomically if tracking a message
            if message_date:
                # Use F() for atomic increment to avoid race conditions
                TelegramUser.objects.filter(pk=user.pk).update(
                    message_count=F('message_count') + 1,
                    last_seen=timezone.now()
                )
                user.refresh_from_db(fields=['message_count'])
                # Remove last_seen from update_fields since we updated it above
                if 'last_seen' in update_fields:
                    update_fields.remove('last_seen')

            # Save other changes if any
            if update_fields:
                user.save(update_fields=update_fields)

        # Record the observed name/handle triple as an alias. Append-only audit:
        # distinct triples create new rows, repeat sightings bump the counter.
        _record_user_alias(user, user_data, channel=channel, message_id=message_id)

        # Track membership - use update_or_create for single query upsert
        if message_date:
            # From message - track message activity with atomic increment
            membership, mem_created = UserGroupMembership.objects.get_or_create(
                user=user,
                channel=channel,
                defaults={
                    'last_message_date': message_date,
                    'message_count': 1,
                    'active': True,
                }
            )
            if not mem_created:
                # Atomic update for existing membership
                UserGroupMembership.objects.filter(pk=membership.pk).update(
                    last_seen=timezone.now(),
                    message_count=F('message_count') + 1,
                    last_message_date=message_date
                )
        else:
            # From participant scan or chat action - track admin status
            membership, mem_created = UserGroupMembership.objects.update_or_create(
                user=user,
                channel=channel,
                defaults={
                    'last_seen': timezone.now(),
                    'is_admin': is_admin,
                    'is_creator': is_creator,
                    'admin_title': admin_title,
                    'active': True,
                }
            )

    return user, created


async def track_user_from_message_async(sender, channel, message_date=None):
    """
    Async wrapper for tracking a user from a message sender.
    Extracts data from Telethon object BEFORE sync_to_async to avoid gevent issues.
    """
    user_data = _extract_user_data(sender)
    if user_data is None:
        return None, False

    # Use thread_sensitive=False for gevent compatibility
    return await sync_to_async(_track_user_from_data, thread_sensitive=False)(
        user_data, channel, message_date=message_date
    )


async def track_user_from_participant_async(participant, channel, is_admin=False, is_creator=False, admin_title=''):
    """
    Async wrapper for tracking a user from a channel participant.
    Extracts data from Telethon object BEFORE sync_to_async to avoid gevent issues.
    """
    user_obj = participant
    if hasattr(participant, 'user'):
        user_obj = participant.user
    user_data = _extract_user_data(user_obj)
    if user_data is None:
        return None, False

    # Use thread_sensitive=False for gevent compatibility
    return await sync_to_async(_track_user_from_data, thread_sensitive=False)(
        user_data, channel, is_admin=is_admin, is_creator=is_creator, admin_title=admin_title
    )


def track_user_from_sender_data(sender_data: dict, channel, message_date=None, message_id=None):
    """
    Sync version of user tracking from pre-extracted sender data.
    Used by the event processor worker.

    Args:
        sender_data: Dict with user info (from event payload)
        channel: TelegramChannel model instance
        message_date: Optional datetime of the message
        message_id: Optional Telegram message_id where the sender was observed

    Returns:
        tuple: (TelegramUser instance, bool created)
    """
    return _track_user_from_data(
        sender_data, channel, message_date=message_date, message_id=message_id
    )


def download_user_profile_photo_sync(account, user_id: int, telegram_user, sender_data: dict):
    """
    Sync version of profile photo download.
    Uses TelegramService in sync mode to download the photo.

    Args:
        account: TelegramAccount model instance
        user_id: Telegram user ID
        telegram_user: TelegramUser model instance
        sender_data: Dict with user info (must include photo_id)

    Returns:
        bool: True if photo was downloaded/updated
    """
    try:
        current_photo_id = sender_data.get('photo_id')
        if not current_photo_id:
            logger.debug(f"User {user_id} has no photo_id in sender_data")
            return False

        # Skip if we already have this exact photo
        if telegram_user.photo_id == current_photo_id and telegram_user.profile_photo_base64:
            logger.debug(f"User {user_id} photo unchanged (id={current_photo_id})")
            return False

        # Download profile photo using TelegramService
        service = TelegramService(account)

        async def do_download():
            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number
            )
            try:
                entity = await service._client.get_entity(user_id)
                if not entity or not hasattr(entity, 'photo') or not entity.photo:
                    return None

                photo_bytes = await service._client.download_profile_photo(entity, file=bytes)
                return photo_bytes
            finally:
                await service.disconnect()

        photo_bytes = run_async(do_download())
        if not photo_bytes:
            return False

        # Encode to base64 and save
        base64_data = base64.b64encode(photo_bytes).decode('utf-8')
        telegram_user.photo_id = current_photo_id
        telegram_user.profile_photo_base64 = base64_data
        telegram_user.profile_photo_updated_at = timezone.now()
        telegram_user.save(update_fields=['photo_id', 'profile_photo_base64', 'profile_photo_updated_at'])

        logger.info(f"Downloaded profile photo for user {user_id} ({len(base64_data)} bytes)")
        return True

    except Exception as e:
        logger.warning(f"Could not download profile photo for user {user_id}: {e}")
        return False


async def download_user_profile_photo_async(client, user_id, telegram_user, sender=None):
    """
    Download a user's profile photo and store as base64.

    Args:
        client: Telethon client
        user_id: Telegram user ID
        telegram_user: TelegramUser model instance
        sender: Optional Telethon User object (avoids extra API call if provided)

    Returns:
        bool: True if photo was downloaded/updated

    Raises:
        FloodWaitError: Re-raised so caller can handle rate limiting
    """
    try:
        # Use sender if provided and has photo info, otherwise fetch entity
        entity = sender
        if entity is None or not hasattr(entity, 'photo') or not entity.photo:
            # Sender doesn't have photo info, fetch full entity
            try:
                entity = await client.get_entity(user_id)
            except FloodWaitError:
                # Re-raise FloodWaitError so caller can handle it
                raise
            except Exception as e:
                logger.debug(f"Could not get entity for user {user_id}: {e}")
                return False

        if not entity or not hasattr(entity, 'photo') or not entity.photo:
            logger.debug(f"User {user_id} has no profile photo")
            return False

        current_photo_id = getattr(entity.photo, 'photo_id', None)
        if not current_photo_id:
            logger.debug(f"User {user_id} photo has no photo_id")
            return False

        # Skip if we already have this exact photo
        if telegram_user.photo_id == current_photo_id and telegram_user.profile_photo_base64:
            logger.debug(f"User {user_id} photo unchanged (id={current_photo_id})")
            return False

        # Download profile photo to bytes
        logger.debug(f"Downloading profile photo for user {user_id} (photo_id={current_photo_id})")
        photo_bytes = await client.download_profile_photo(entity, file=bytes)
        if not photo_bytes:
            logger.warning(f"Failed to download profile photo for user {user_id} - empty response")
            return False

        # Encode to base64
        base64_data = base64.b64encode(photo_bytes).decode('utf-8')

        # Update the user record
        @sync_to_async
        def save_photo():
            telegram_user.photo_id = current_photo_id
            telegram_user.profile_photo_base64 = base64_data
            telegram_user.profile_photo_updated_at = timezone.now()
            telegram_user.save(update_fields=['photo_id', 'profile_photo_base64', 'profile_photo_updated_at'])

        await save_photo()
        logger.info(f"Downloaded profile photo for user {user_id} ({len(base64_data)} bytes)")
        return True

    except FloodWaitError:
        raise
    except Exception as e:
        logger.warning(f"Could not download profile photo for user {user_id}: {e}")
        return False
