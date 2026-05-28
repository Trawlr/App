"""
Telegram service for handling Telethon operations.
"""

import asyncio
import logging
from typing import Optional

from asgiref.sync import sync_to_async
from gevent import monkey
from gevent.threadpool import ThreadPool
from telethon import TelegramClient
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    InviteHashEmptyError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    UserAlreadyParticipantError,
    UserNotParticipantError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.account import ReportPeerRequest
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    DeleteChatUserRequest,
    GetSearchCountersRequest,
    ImportChatInviteRequest,
    ReportRequest,
)
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (
    InputMessagesFilterDocument,
    InputMessagesFilterGif,
    InputMessagesFilterPhotos,
    InputMessagesFilterRoundVideo,
    InputMessagesFilterVideo,
    InputMessagesFilterVoice,
    InputPeerChannel,
    InputPeerUser,
    InputReportReasonChildAbuse,
    InputReportReasonCopyright,
    InputReportReasonFake,
    InputReportReasonIllegalDrugs,
    InputReportReasonOther,
    InputReportReasonPersonalDetails,
    InputReportReasonPornography,
    InputReportReasonSpam,
    InputReportReasonViolence,
    InputUser,
    PeerChannel,
    ReportResultAddComment,
    ReportResultChooseOption,
    ReportResultReported,
)

logger = logging.getLogger('trawlr.telegram')


class TelegramService:
    """
    Service for managing Telegram client operations.

    Handles authentication, session management, and API interactions.
    """

    def __init__(self, account=None):
        """
        Initialize the service.

        Args:
            account: TelegramAccount model instance (optional)
        """
        self.account = account
        self._client: Optional[TelegramClient] = None
        self._phone_code_hash: Optional[str] = None

    async def create_client(
        self,
        api_id: str,
        api_hash: str,
        phone_number: str,
        receive_updates: bool = False
    ) -> TelegramClient:
        """
        Create a new Telegram client using StringSession stored in PostgreSQL.

        Args:
            api_id: Telegram API ID
            api_hash: Telegram API hash
            phone_number: Phone number for the account
            receive_updates: Whether to receive real-time updates (True for listeners)

        Returns:
            TelegramClient instance
        """
        session_string = ""
        if self.account and self.account.session_string:
            # Strip any whitespace that might have been added during storage
            session_string = self.account.session_string.strip()
            logger.debug("Creating client with existing session")
        else:
            logger.debug("Creating client with new empty session")

        self._client = TelegramClient(
            StringSession(session_string),
            int(api_id),
            api_hash,
            device_model="Trawlr",
            system_version="1.0",
            app_version="1.0",
            timeout=30,  # Connection timeout
            request_retries=2,
            connection_retries=2,
            receive_updates=receive_updates,
        )

        try:
            await self._client.connect()
            logger.debug("Client connected")
        except (AuthKeyDuplicatedError, AuthKeyUnregisteredError) as e:
            # Session is invalid - clear it and retry with empty session
            logger.warning(f"Session invalid ({type(e).__name__}), clearing and retrying with fresh session")
            await self._clear_session_and_reconnect(api_id, api_hash, receive_updates)
        except Exception as e:
            # Check if this is a session/auth key error by message
            error_msg = str(e).lower()
            if 'auth' in error_msg and ('key' in error_msg or 'duplicat' in error_msg or 'invalid' in error_msg):
                logger.warning(f"Session appears invalid ({e}), clearing and retrying with fresh session")
                await self._clear_session_and_reconnect(api_id, api_hash, receive_updates)
            else:
                raise

        return self._client

    async def _clear_session_and_reconnect(self, api_id: str, api_hash: str, receive_updates: bool):
        """Clear invalid session and create a new client."""
        if self.account:
            self.account.session_string = ''
            self.account.is_authenticated = False
            self.account.save(update_fields=['session_string', 'is_authenticated'])

        # Create a new client with empty session
        self._client = TelegramClient(
            StringSession(),
            int(api_id),
            api_hash,
            device_model="Trawlr",
            system_version="1.0",
            app_version="1.0",
            timeout=30,
            request_retries=2,
            connection_retries=2,
            receive_updates=receive_updates,
        )
        await self._client.connect()
        logger.debug("Client connected with fresh session")

    async def save_session(self):
        """Save the current session string to the database."""
        if self._client and self.account:
            logger.info("Saving session to database...")
            session_string = self._client.session.save()
            self.account.session_string = session_string
            await sync_to_async(self.account.save)(update_fields=['session_string'])
            logger.info("Session saved to database")

    async def send_code(self, phone_number: str) -> dict:
        """
        Send verification code to phone number.

        Args:
            phone_number: Phone number with country code

        Returns:
            dict with status and any error messages
        """
        try:
            result = await self._client.send_code_request(phone_number)
            self._phone_code_hash = result.phone_code_hash
            return {
                'success': True,
                'phone_code_hash': result.phone_code_hash,
            }
        except PhoneNumberInvalidError:
            return {
                'success': False,
                'error': 'Invalid phone number format.',
            }
        except (AuthKeyDuplicatedError, AuthKeyUnregisteredError) as e:
            # Session was invalidated - clear it and ask user to retry
            logger.warning(f"Session invalid during send_code ({type(e).__name__}), clearing session")
            if self.account:
                self.account.session_string = ''
                self.account.is_authenticated = False
                # Save synchronously - we're running in a thread created by run_async
                self.account.save(update_fields=['session_string', 'is_authenticated'])
            return {
                'success': False,
                'error': 'Session was invalidated. Please try again.',
                'session_cleared': True,
            }
        except FloodWaitError as e:
            return {
                'success': False,
                'error': f'Too many attempts. Please wait {e.seconds} seconds.',
                'flood_wait': e.seconds,
            }
        except Exception as e:
            logger.exception("Error sending code")
            return {
                'success': False,
                'error': str(e),
            }

    async def verify_code(
        self,
        phone_number: str,
        code: str,
        phone_code_hash: Optional[str] = None
    ) -> dict:
        """
        Verify the code sent to the phone.

        Args:
            phone_number: Phone number with country code
            code: Verification code
            phone_code_hash: Phone code hash from send_code

        Returns:
            dict with status, may indicate 2FA is needed
        """
        if phone_code_hash is None:
            phone_code_hash = self._phone_code_hash

        try:
            await self._client.sign_in(
                phone_number,
                code,
                phone_code_hash=phone_code_hash
            )
            # Session is saved by the view after this returns
            return {
                'success': True,
                'authenticated': True,
            }
        except SessionPasswordNeededError:
            return {
                'success': True,
                'authenticated': False,
                'requires_2fa': True,
            }
        except PhoneCodeInvalidError:
            return {
                'success': False,
                'error': 'Invalid verification code.',
            }
        except PhoneCodeExpiredError:
            return {
                'success': False,
                'error': 'Verification code has expired. Please request a new one.',
            }
        except FloodWaitError as e:
            return {
                'success': False,
                'error': f'Too many attempts. Please wait {e.seconds} seconds.',
                'flood_wait': e.seconds,
            }
        except Exception as e:
            logger.exception("Error verifying code")
            return {
                'success': False,
                'error': str(e),
            }

    async def verify_password(self, password: str) -> dict:
        """
        Verify 2FA password.

        Args:
            password: The 2FA password

        Returns:
            dict with status
        """
        try:
            # Check if already authorized (session might be complete)
            if await self._client.is_user_authorized():
                logger.info("User is already authorized, skipping 2FA")
                return {
                    'success': True,
                    'authenticated': True,
                }

            logger.info("Starting 2FA sign_in...")
            await self._client.sign_in(password=password)
            logger.info("2FA sign_in completed successfully")
            return {
                'success': True,
                'authenticated': True,
            }
        except Exception as e:
            logger.exception("Error verifying 2FA password")
            return {
                'success': False,
                'error': 'Invalid 2FA password.',
            }

    async def is_authorized(self) -> bool:
        """Check if the client is authorized."""
        if self._client is None:
            return False
        return await self._client.is_user_authorized()

    async def get_me(self):
        """Get the current user info."""
        if self._client is None:
            return None
        return await self._client.get_me()

    async def get_dialogs(self, limit: int = 100):
        """
        Get all dialogs (channels, groups, chats).

        Args:
            limit: Maximum number of dialogs to fetch

        Returns:
            List of dialogs
        """
        if self._client is None:
            return []
        return await self._client.get_dialogs(limit=limit)

    async def join_channel(self, url_or_username: str) -> dict:
        """
        Join a channel or group by URL or username.

        Supports formats:
        - t.me/username
        - https://t.me/username
        - @username
        - username
        - t.me/+invitehash (private invite)
        - t.me/joinchat/invitehash (old format)

        Returns:
            dict with 'success', 'entity' or 'error' keys
        """
        if self._client is None:
            return {'success': False, 'error': 'Client not connected'}

        # Clean up the input
        url = url_or_username.strip()

        # Extract username or invite hash from URL
        invite_hash = None
        username = None

        # Check for invite links
        if '/+' in url or '/joinchat/' in url:
            # Private invite link
            if '/+' in url:
                invite_hash = url.split('/+')[-1].split('?')[0].split('/')[0]
            else:
                invite_hash = url.split('/joinchat/')[-1].split('?')[0].split('/')[0]
        elif 't.me/' in url:
            # Public channel/group
            username = url.split('t.me/')[-1].split('?')[0].split('/')[0]
        elif url.startswith('@'):
            username = url[1:]
        else:
            # Assume it's a username
            username = url

        try:
            if invite_hash:
                # Join via invite hash
                try:
                    # First check the invite
                    invite_info = await self._client(CheckChatInviteRequest(invite_hash))
                    logger.info(f"Invite info: {invite_info}")
                except Exception as e:
                    logger.warning(f"Could not check invite: {e}")

                updates = await self._client(ImportChatInviteRequest(invite_hash))
                # Get the chat from the updates
                if hasattr(updates, 'chats') and updates.chats:
                    entity = updates.chats[0]
                    return {
                        'success': True, 'entity': entity,
                        'title': getattr(entity, 'title', 'Unknown'),
                        'invite_hash': invite_hash, 'source_url': url,
                    }
                return {
                    'success': True, 'entity': None, 'title': 'Joined successfully',
                    'invite_hash': invite_hash, 'source_url': url,
                }

            else:
                # Join public channel/group by username
                entity = await self._client.get_entity(username)
                await self._client(JoinChannelRequest(entity))
                return {
                    'success': True, 'entity': entity,
                    'title': getattr(entity, 'title', username),
                    'invite_hash': '', 'source_url': url,
                }

        except UserAlreadyParticipantError:
            return {'success': False, 'error': 'Already a member of this channel/group'}
        except InviteHashExpiredError:
            return {'success': False, 'error': 'Invite link has expired'}
        except InviteHashInvalidError:
            return {'success': False, 'error': 'Invalid invite link'}
        except ChannelPrivateError:
            return {'success': False, 'error': 'This channel is private'}
        except FloodWaitError as e:
            return {'success': False, 'error': f'Rate limited. Try again in {e.seconds} seconds'}
        except Exception as e:
            logger.exception(f"Error joining channel: {e}")
            return {'success': False, 'error': str(e)}

    async def report_peer(self, peer_id: int, reason: str, message: str = '', username: str = None) -> dict:
        """
        Report a channel, group, or user to Telegram.

        Args:
            peer_id: Telegram ID of the entity to report
            reason: One of: spam, violence, pornography, child_abuse, copyright,
                    fake, illegal_drugs, personal_details, other
            message: Optional additional context (required for 'other')
            username: Optional username to help resolve the entity

        Returns:
            {'success': True} or {'success': False, 'error': str}
        """
        if self._client is None:
            return {'success': False, 'error': 'Client not connected'}

        reason_map = {
            'spam': InputReportReasonSpam(),
            'violence': InputReportReasonViolence(),
            'pornography': InputReportReasonPornography(),
            'child_abuse': InputReportReasonChildAbuse(),
            'copyright': InputReportReasonCopyright(),
            'fake': InputReportReasonFake(),
            'illegal_drugs': InputReportReasonIllegalDrugs(),
            'personal_details': InputReportReasonPersonalDetails(),
            'other': InputReportReasonOther(),
        }

        report_reason = reason_map.get(reason)
        if not report_reason:
            return {'success': False, 'error': f'Invalid reason: {reason}'}

        try:
            # Try to get entity by username first (more reliable), then by ID
            entity = None
            if username:
                try:
                    entity = await self._client.get_entity(username)
                except Exception:
                    logger.warning(f"Could not get entity by username {username}, trying by ID")

            if entity is None:
                # Try to find in dialogs (populates internal cache)
                async for dialog in self._client.iter_dialogs():
                    if getattr(dialog.entity, 'id', None) == peer_id:
                        entity = dialog.entity
                        break

            if entity is None:
                return {'success': False, 'error': 'Could not find the channel/user. Make sure you are a member.'}

            await self._client(ReportPeerRequest(
                peer=entity,
                reason=report_reason,
                message=message
            ))
            logger.info(f"Reported peer {peer_id} for {reason}")
            return {'success': True}
        except FloodWaitError as e:
            return {'success': False, 'error': f'Rate limited. Try again in {e.seconds} seconds'}
        except Exception as e:
            logger.exception(f"Error reporting peer: {e}")
            return {'success': False, 'error': str(e)}

    async def report_messages(self, peer_id: int, message_ids: list, reason: str, message: str = '', username: str = None) -> dict:
        """
        Report specific messages to Telegram using the messages.report API.

        The API works iteratively:
        1. Call with empty option to get available options (ReportResultChooseOption)
        2. Select matching option based on reason
        3. Call again with selected option
        4. May need to add comment (ReportResultAddComment)
        5. Finally get ReportResultReported on success

        Args:
            peer_id: Telegram ID of the channel/chat containing the messages
            message_ids: List of message IDs to report
            reason: Report reason key (spam, violence, pornography, child_abuse, etc.)
            message: Optional additional context
            username: Optional username to help resolve the entity

        Returns:
            {'success': True} or {'success': False, 'error': str}
        """
        if self._client is None:
            return {'success': False, 'error': 'Client not connected'}

        # Map our reason keys to keywords that appear in Telegram's report option text
        reason_keywords = {
            'spam': ['spam'],
            'violence': ['violen'],
            'pornography': ['porn', 'adult', 'sexual', 'explicit', 'nsfw'],
            'child_abuse': ['child', 'minor', 'csam', 'underage'],
            'copyright': ['copyright', 'intellectual'],
            'fake': ['fake', 'impersonat', 'scam', 'fraud'],
            'illegal_drugs': ['drug', 'illegal substance'],
            'personal_details': ['personal', 'dox', 'privacy'],
            'other': ['other'],
        }

        keywords = reason_keywords.get(reason)
        if not keywords:
            return {'success': False, 'error': f'Invalid reason: {reason}'}

        try:

            # Try multiple methods to resolve the entity
            entity = None

            # 1. Try by username first (most reliable if available)
            if username and entity is None:
                try:
                    entity = await self._client.get_entity(username)
                    logger.debug(f"Resolved entity by username: {username}")
                except Exception as e:
                    logger.warning(f"Could not get entity by username {username}: {e}")

            # 2. Try to get entity directly by ID
            if entity is None:
                try:
                    entity = await self._client.get_entity(peer_id)
                    logger.debug(f"Resolved entity by peer_id: {peer_id}")
                except Exception as e:
                    logger.warning(f"Could not get entity by peer_id {peer_id}: {e}")

            # 3. Try with PeerChannel (for channel IDs)
            if entity is None:
                try:
                    entity = await self._client.get_entity(PeerChannel(peer_id))
                    logger.debug(f"Resolved entity by PeerChannel: {peer_id}")
                except Exception as e:
                    logger.warning(f"Could not get entity by PeerChannel {peer_id}: {e}")

            # 4. Try with negative ID format (Telegram channel format: -100XXXXXXXXXX)
            if entity is None and peer_id > 0:
                try:
                    negative_id = int(f"-100{peer_id}")
                    entity = await self._client.get_entity(negative_id)
                    logger.debug(f"Resolved entity by negative ID: {negative_id}")
                except Exception as e:
                    logger.warning(f"Could not get entity by negative ID: {e}")

            # 5. Fall back to searching dialogs
            if entity is None:
                logger.debug(f"Searching dialogs for peer_id: {peer_id}")
                async for dialog in self._client.iter_dialogs():
                    dialog_id = getattr(dialog.entity, 'id', None)
                    if dialog_id == peer_id or dialog_id == abs(peer_id):
                        entity = dialog.entity
                        logger.debug(f"Found entity in dialogs: {dialog_id}")
                        break

            if entity is None:
                return {
                    'success': False,
                    'error': 'Could not find the channel/user. It may have been deleted, made private, or you are no longer a member.'
                }

            # Start with empty option to get available choices
            current_option = b''
            max_iterations = 5  # Prevent infinite loops

            for _ in range(max_iterations):
                result = await self._client(ReportRequest(
                    peer=entity,
                    id=message_ids,
                    option=current_option,
                    message=message
                ))

                if isinstance(result, ReportResultReported):
                    # Success!
                    logger.info(f"Reported messages {message_ids} in peer {peer_id} for {reason}")
                    return {'success': True}

                elif isinstance(result, ReportResultChooseOption):
                    # Need to select an option
                    selected_option = None

                    # Find matching option based on reason keywords
                    for opt in result.options:
                        opt_text = opt.text.lower() if hasattr(opt, 'text') else ''
                        for kw in keywords:
                            if kw in opt_text:
                                selected_option = opt.option
                                logger.debug(f"Selected report option '{opt.text}' for reason '{reason}'")
                                break
                        if selected_option:
                            break

                    # If no exact match and reason is 'other', use last option
                    if not selected_option and reason == 'other' and result.options:
                        selected_option = result.options[-1].option

                    # Fallback to first option if no match
                    if not selected_option and result.options:
                        logger.warning(f"No exact match for reason '{reason}', using first option: {result.options[0].text}")
                        selected_option = result.options[0].option

                    if not selected_option:
                        return {'success': False, 'error': 'No report options available'}

                    current_option = selected_option

                elif isinstance(result, ReportResultAddComment):
                    # Telegram wants a comment - use the option from the result
                    # and include our message in the next request
                    logger.debug(f"Telegram requested additional comment (optional={result.optional})")
                    current_option = result.option
                    # The message parameter is already being passed, continue to next iteration

                else:
                    logger.warning(f"Unexpected report result type: {type(result)}")
                    return {'success': False, 'error': 'Unexpected response from Telegram'}

            return {'success': False, 'error': 'Report flow did not complete'}

        except FloodWaitError as e:
            return {'success': False, 'error': f'Rate limited. Try again in {e.seconds} seconds'}
        except Exception as e:
            logger.exception(f"Error reporting messages: {e}")
            return {'success': False, 'error': str(e)}

    async def leave_channel(self, peer_id: int, username: str = None) -> dict:
        """
        Leave a channel or group.

        Args:
            peer_id: Telegram ID of the channel/group
            username: Optional username to help resolve the entity

        Returns:
            {'success': True} or {'success': False, 'error': str}
        """
        if self._client is None:
            return {'success': False, 'error': 'Client not connected'}

        try:
            # Try to get entity by username first (more reliable), then by ID
            entity = None
            if username:
                try:
                    entity = await self._client.get_entity(username)
                except Exception:
                    logger.debug(f"Could not get entity by username {username}, trying by ID")

            if entity is None:
                try:
                    entity = await self._client.get_entity(peer_id)
                except Exception:
                    logger.debug(f"Could not get entity by ID {peer_id}")

            if entity is None:
                try:
                    entity = await self._client.get_entity(PeerChannel(peer_id))
                except Exception:
                    logger.debug(f"Could not get entity by PeerChannel {peer_id}")

            # Last resort: fetch dialogs to populate entity cache, then retry
            if entity is None:
                try:
                    logger.debug(f"Fetching dialogs to populate entity cache for {peer_id}")
                    await self._client.get_dialogs()
                    entity = await self._client.get_entity(PeerChannel(peer_id))
                except Exception:
                    logger.debug(f"Still could not resolve {peer_id} after fetching dialogs")

            if entity is None:
                return {'success': False, 'error': 'Could not find the channel. It may have been deleted or you are not a member.'}

            # Use LeaveChannelRequest for channels and supergroups
            await self._client(LeaveChannelRequest(entity))
            logger.info(f"Left channel/group: {peer_id}")
            return {'success': True}

        except UserNotParticipantError:
            # Already not a participant - treat as success
            logger.info(f"Already not a participant of {peer_id}")
            return {'success': True}
        except ChannelPrivateError:
            return {'success': False, 'error': 'Cannot access this channel (private or restricted)'}
        except FloodWaitError as e:
            return {'success': False, 'error': f'Rate limited. Try again in {e.seconds} seconds'}
        except Exception as e:
            logger.exception(f"Error leaving channel: {e}")
            return {'success': False, 'error': str(e)}

    async def get_media_counts(self, peer_id: int, username: str = None) -> dict:
        """
        Get media file counts for a channel/group using GetSearchCountersRequest.

        Args:
            peer_id: Telegram ID of the channel/group
            username: Optional username to help resolve the entity

        Returns:
            {'success': True, 'counts': {'photos': N, 'videos': N, 'files': N}}
            or {'success': False, 'error': str}
        """
        if self._client is None:
            return {'success': False, 'error': 'Client not connected'}

        try:
            # Try to get entity by username first (more reliable), then by ID
            entity = None
            if username:
                try:
                    entity = await self._client.get_entity(username)
                except Exception:
                    logger.debug(f"Could not get entity by username {username}, trying by ID")

            if entity is None:
                # Try to get entity by peer_id directly
                try:
                    entity = await self._client.get_entity(peer_id)
                except Exception:
                    logger.debug(f"Could not get entity by ID {peer_id}, trying input entity")

            if entity is None:
                # Try input entity (uses internal cache)
                try:
                    entity = await self._client.get_input_entity(peer_id)
                except Exception:
                    logger.debug(f"Could not get input entity for {peer_id}")

            if entity is None:
                # Fall back to searching dialogs (populates cache)
                logger.debug(f"Searching dialogs for peer_id: {peer_id}")
                async for dialog in self._client.iter_dialogs():
                    dialog_id = getattr(dialog.entity, 'id', None)
                    if dialog_id == peer_id or dialog_id == abs(peer_id):
                        entity = dialog.entity
                        logger.debug(f"Found entity in dialogs: {dialog_id}")
                        break

            if entity is None:
                return {'success': False, 'error': 'Could not find the channel. Make sure you are a member.'}

            counters = await self._client(GetSearchCountersRequest(
                peer=entity,
                filters=[
                    InputMessagesFilterPhotos(),
                    InputMessagesFilterVideo(),
                    InputMessagesFilterDocument(),
                    InputMessagesFilterVoice(),
                    InputMessagesFilterRoundVideo(),
                    InputMessagesFilterGif(),
                ]
            ))

            # Map filter types to counts
            counts = {
                'photos': 0,
                'videos': 0,
                'files': 0,  # Aggregate: documents + voice + round + gif
            }

            for counter in counters:
                filter_name = type(counter.filter).__name__
                if filter_name == 'InputMessagesFilterPhotos':
                    counts['photos'] = counter.count
                elif filter_name == 'InputMessagesFilterVideo':
                    counts['videos'] = counter.count
                elif filter_name in ('InputMessagesFilterDocument', 'InputMessagesFilterVoice',
                                      'InputMessagesFilterRoundVideo', 'InputMessagesFilterGif'):
                    counts['files'] += counter.count

            logger.info(f"Got media counts for {peer_id}: {counts}")
            return {'success': True, 'counts': counts}

        except FloodWaitError as e:
            return {'success': False, 'error': f'Rate limited. Wait {e.seconds}s'}
        except Exception as e:
            logger.exception(f"Error getting media counts for {peer_id}")
            return {'success': False, 'error': str(e)}

    async def get_full_user(self, telegram_id: int, access_hash: int) -> dict:
        """
        Get full user profile using GetFullUserRequest.

        Args:
            telegram_id: Telegram user ID
            access_hash: User's access hash for API calls

        Returns:
            {'success': True, 'data': {...}} with profile fields
            or {'success': False, 'error': str}
        """

        if self._client is None:
            return {'success': False, 'error': 'Client not connected'}

        try:
            input_user = InputUser(user_id=telegram_id, access_hash=access_hash)
            result = await self._client(GetFullUserRequest(id=input_user))

            full_user = result.full_user

            # Extract business work hours if present
            business_work_hours = None
            if hasattr(full_user, 'business_work_hours') and full_user.business_work_hours:
                try:
                    business_work_hours = {
                        'timezone': getattr(full_user.business_work_hours, 'timezone_id', None),
                        'open_now': getattr(full_user.business_work_hours, 'open_now', False),
                    }
                except Exception:
                    pass

            # Extract business location if present
            business_location = ''
            if hasattr(full_user, 'business_location') and full_user.business_location:
                business_location = getattr(full_user.business_location, 'address', '') or ''

            # Extract business intro if present
            business_intro = ''
            if hasattr(full_user, 'business_intro') and full_user.business_intro:
                business_intro = getattr(full_user.business_intro, 'description', '') or ''

            # Extract birthday if present
            birthday = ''
            if hasattr(full_user, 'birthday') and full_user.birthday:
                bd = full_user.birthday
                if hasattr(bd, 'day') and hasattr(bd, 'month'):
                    year = getattr(bd, 'year', None)
                    if year:
                        birthday = f"{bd.day}/{bd.month}/{year}"
                    else:
                        birthday = f"{bd.day}/{bd.month}"

            data = {
                'bio': full_user.about or '',
                'birthday': birthday,
                'private_forward_name': getattr(full_user, 'private_forward_name', '') or '',
                'personal_channel_id': getattr(full_user, 'personal_channel_id', None),
                'common_chats_count': full_user.common_chats_count or 0,
                'phone_calls_available': getattr(full_user, 'phone_calls_available', False),
                'video_calls_available': getattr(full_user, 'video_calls_available', False),
                'voice_messages_forbidden': getattr(full_user, 'voice_messages_forbidden', False),
                'contact_require_premium': getattr(full_user, 'contact_require_premium', False),
                'is_blocked': full_user.blocked or False,
                'business_intro': business_intro,
                'business_location': business_location,
                'business_work_hours': business_work_hours,
                'has_pinned_stories': getattr(full_user, 'stories_pinned_available', False),
                'has_scheduled_messages': getattr(full_user, 'has_scheduled', False),
                'pinned_message_id': getattr(full_user, 'pinned_msg_id', None),
            }

            logger.info(f"Got full profile for user {telegram_id}: bio={data['bio'][:50] if data['bio'] else '(none)'}...")
            return {'success': True, 'data': data}

        except FloodWaitError as e:
            return {'success': False, 'error': f'Rate limited. Wait {e.seconds}s', 'flood_wait': e.seconds}
        except ValueError as e:
            # Entity not found - user may have deleted account, blocked us, or access_hash is stale
            logger.debug(f"Could not resolve entity for user {telegram_id}: {e}")
            return {'success': False, 'error': 'User not accessible'}
        except Exception as e:
            logger.exception(f"Error getting full user profile for {telegram_id}")
            return {'success': False, 'error': str(e)}

    async def resolve_entity(self, identifier: str, link_type: str) -> dict:
        """
        Resolve a t.me link identifier to entity info.

        Args:
            identifier: Username or invite hash
            link_type: 'username' or 'invite'

        Returns:
            dict with 'success', 'entity_type', 'data' keys
        """
        if self._client is None:
            return {'success': False, 'error': 'Client not connected'}

        try:
            if link_type == 'invite':
                invite_info = await self._client(CheckChatInviteRequest(identifier))
                # ChatInvite or ChatInviteAlready
                if hasattr(invite_info, 'chat'):
                    # Already a member - we have full chat info
                    chat = invite_info.chat
                    return {
                        'success': True,
                        'entity_type': 'channel',
                        'data': {
                            'id': chat.id,
                            'title': getattr(chat, 'title', ''),
                            'username': getattr(chat, 'username', None),
                            'participants_count': getattr(chat, 'participants_count', None),
                            'megagroup': getattr(chat, 'megagroup', False),
                        },
                    }
                else:
                    # Not a member - limited preview
                    return {
                        'success': True,
                        'entity_type': 'invite',
                        'data': {
                            'title': getattr(invite_info, 'title', ''),
                            'participants_count': getattr(invite_info, 'participants_count', None),
                            'about': getattr(invite_info, 'about', ''),
                            'megagroup': getattr(invite_info, 'megagroup', False),
                            'broadcast': getattr(invite_info, 'broadcast', False),
                            'request_needed': getattr(invite_info, 'request_needed', False),
                        },
                    }
            else:
                # Username resolution
                entity = await self._client.get_entity(identifier)
                entity_type_name = type(entity).__name__

                if entity_type_name == 'User':
                    return {
                        'success': True,
                        'entity_type': 'bot' if getattr(entity, 'bot', False) else 'user',
                        'data': {
                            'id': entity.id,
                            'first_name': getattr(entity, 'first_name', '') or '',
                            'last_name': getattr(entity, 'last_name', '') or '',
                            'username': getattr(entity, 'username', None),
                            'bot': getattr(entity, 'bot', False),
                            'verified': getattr(entity, 'verified', False),
                            'premium': getattr(entity, 'premium', False),
                            'phone': getattr(entity, 'phone', None),
                        },
                    }
                else:
                    # Channel or Chat
                    return {
                        'success': True,
                        'entity_type': 'channel',
                        'data': {
                            'id': entity.id,
                            'title': getattr(entity, 'title', ''),
                            'username': getattr(entity, 'username', None),
                            'participants_count': getattr(entity, 'participants_count', None),
                            'megagroup': getattr(entity, 'megagroup', False),
                            'broadcast': getattr(entity, 'broadcast', False),
                            'verified': getattr(entity, 'verified', False),
                            'scam': getattr(entity, 'scam', False),
                            'fake': getattr(entity, 'fake', False),
                        },
                    }

        except InviteHashExpiredError:
            return {'success': False, 'error': 'Invite link has expired'}
        except InviteHashInvalidError:
            return {'success': False, 'error': 'Invalid invite link'}
        except ChannelPrivateError:
            return {'success': False, 'error': 'Channel is private'}
        except FloodWaitError as e:
            return {'success': False, 'error': f'Rate limited. Wait {e.seconds}s', 'flood_wait': e.seconds}
        except ValueError:
            return {'success': False, 'error': 'Entity not found'}
        except Exception as e:
            logger.exception(f"Error resolving entity '{identifier}'")
            return {'success': False, 'error': str(e)}

    async def disconnect(self):
        """Disconnect the client."""
        if self._client:
            logger.info("Disconnecting client...")
            await self._client.disconnect()
            self._client = None
            logger.info("Client disconnected")

    def __del__(self):
        """Cleanup on deletion."""
        if self._client and self._client.is_connected():
            try:
                # Use new_event_loop to avoid gevent issues
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self._client.disconnect())
                finally:
                    loop.close()
            except Exception:
                pass


def _is_gevent_active():
    """Check if gevent has monkey-patched the system."""
    try:
        return monkey.is_module_patched('socket')
    except ImportError:
        return False


def _run_in_thread(coro):
    """
    Run a coroutine in a real native OS thread with its own event loop.

    Uses gevent.threadpool.ThreadPool which creates actual native OS threads
    that are NOT affected by gevent's monkey-patching. This is critical because
    concurrent.futures.ThreadPoolExecutor creates greenlets (not real threads)
    when gevent is active, which still share the same event loop.
    """

    def run_coro():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            try:
                loop.close()
            except Exception:
                pass

    pool = ThreadPool(1)
    try:
        return pool.apply(run_coro)
    finally:
        pool.kill()


def run_async(coro):
    """
    Run an async coroutine from sync code.

    When gevent is active (dramatiq-gevent worker), always uses a separate thread
    to avoid event loop conflicts. Otherwise runs directly.
    """
    # If gevent is active, always use thread pool to avoid conflicts
    if _is_gevent_active():
        return _run_in_thread(coro)

    # No gevent - try direct approach
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        # Already in async context - use thread pool
        return _run_in_thread(coro)
    else:
        # No running loop, create a fresh one directly
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            try:
                loop.close()
            except Exception:
                pass
