"""
Accounts app views.
"""

import logging
import os
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST
from rest_framework.authtoken.models import Token
from telethon.tl.types import Channel, Chat

from audit.models import ChannelConfig, ExclusionRule, TelegramChannel, TelegramUser
from downloads.models import DownloadTask, TaskRun
from listener_service.signals import start_listener, stop_listener
from tasks import (
    check_all_source_availability,
    dispatch_task,
    get_dead_letter_stats,
    process_download_queue,
    purge_dead_letters,
    recover_stuck_tasks,
    refresh_all_channel_stats,
    refresh_all_media_counts,
    requeue_dead_letters,
    run_channel_onboarding,
    sync_account_channels,
    sync_all_account_channels,
    sync_all_forum_topics,
)

from .forms import (
    TelegramAccountForm,
    TelegramAccountSettingsForm,
    TelegramCodeForm,
    TelegramPasswordForm,
)
from .models import GlobalSettings, TelegramAccount
from .telegram_service import TelegramService, run_async

logger = logging.getLogger('trawlr.accounts')

def _json_response(success, message=None, error=None, data=None, status=200):
    """
    Create a standardized JSON response.
    Args:
        success: Boolean indicating success/failure
        message: Success message (for success=True)
        error: Error message (for success=False)
        data: Additional data dict to include in response
        status: HTTP status code
    Returns:
        JsonResponse with consistent format:
        - Success: {'success': True, 'message': '...', ...data}
        - Failure: {'success': False, 'error': '...', ...data}
    """
    response = {'success': success}
    if success and message:
        response['message'] = message
    elif not success and error:
        response['error'] = error
    if data:
        response.update(data)
    return JsonResponse(response, status=status)


# Session key templates for Telegram authentication flow
SESSION_KEY_AUTH_STEP = 'telegram_auth_step_{}'
SESSION_KEY_PHONE_HASH = 'telegram_phone_code_hash_{}'
SESSION_KEY_SESSION_STRING = 'telegram_session_string_{}'


def _sync_dialogs_to_db(account, dialogs, run_onboarding=False):
    """
    Sync Telegram dialogs to the database.

    Args:
        account: TelegramAccount instance
        dialogs: List of Telethon Dialog objects
        run_onboarding: If True, trigger onboarding tasks for newly created channels

    Returns:
        Number of channels sync'd
    """
    synced_count = 0
    for dialog in dialogs:
        entity = dialog.entity

        if isinstance(entity, Channel):
            if entity.megagroup:
                channel_type = 'supergroup'
            else:
                channel_type = 'channel'
            is_private = not entity.username
        elif isinstance(entity, Chat):
            channel_type = 'group'
            is_private = True
        else:
            continue

        channel, created = TelegramChannel.objects.update_or_create(
            telegram_id=entity.id,
            defaults={
                'account': account,
                'title': dialog.name or 'Unknown',
                'username': getattr(entity, 'username', None),
                'channel_type': channel_type,
                'is_private': is_private,
                'member_count': getattr(entity, 'participants_count', 0) or 0,
                'joined_at': getattr(entity, 'date', None),
                'is_forum': getattr(entity, 'forum', False) or False,
            }
        )

        # Create config if needed
        if created:
            ChannelConfig.objects.get_or_create(channel=channel)
            # Trigger onboarding if requested and not already onboarded
            if run_onboarding and not channel.onboarded:
                channel.onboarded = True
                channel.save(update_fields=['onboarded'])
                run_channel_onboarding.send(channel.pk)
                logger.info(f"Queued onboarding for new channel: {channel.title}")

        synced_count += 1

    return synced_count


def _get_session_with_fallback(request, account):
    """
    Get session string from database with Django session fallback.

    Refreshes account from DB and uses Django session as fallback if DB is empty.

    Args:
        request: HTTP request object
        account: TelegramAccount instance

    Returns:
        The account instance (modified in-place if fallback was used)
    """
    account.refresh_from_db()
    django_session_string = request.session.get(SESSION_KEY_SESSION_STRING.format(account.pk))
    if not account.session_string and django_session_string:
        logger.warning(f"Using Django session fallback for account {account.pk}")
        account.session_string = django_session_string
    return account


@login_required
def telegram_accounts(request):
    """List all Telegram accounts."""
    accounts = TelegramAccount.objects.active()
    return render(request, 'accounts/telegram_accounts.html', {'accounts': accounts})


@login_required
def telegram_account_add(request):
    """Add a new Telegram account - Step 1: Enter credentials."""
    if request.method == 'POST':
        form = TelegramAccountForm(request.POST)
        if form.is_valid():
            # Create account but don't save yet (not authenticated)
            account = form.save(commit=False)
            account.user = request.user

            # Save the account
            account.save()

            # Redirect to auth flow
            return redirect('accounts:telegram_account_auth', pk=account.pk)
    else:
        form = TelegramAccountForm()

    return render(request, 'accounts/telegram_account_add.html', {'form': form})


@login_required
def telegram_account_detail(request, pk):
    """View/edit a Telegram account."""
    account = get_object_or_404(TelegramAccount, pk=pk)

    if request.method == 'POST':
        form = TelegramAccountSettingsForm(request.POST, instance=account)
        if form.is_valid():
            form.save()
            messages.success(request, 'Account settings updated.')
            return redirect('accounts:telegram_account_detail', pk=pk)
    else:
        form = TelegramAccountSettingsForm(instance=account)

    # Get channel count for this account
    channel_count = account.channels.count() if hasattr(account, 'channels') else 0

    return render(request, 'accounts/telegram_account_detail.html', {
        'account': account,
        'form': form,
        'channel_count': channel_count,
    })


@login_required
def telegram_account_auth(request, pk):
    """Telegram authentication wizard."""
    account = get_object_or_404(TelegramAccount, pk=pk)

    # If already authenticated, redirect
    if account.is_authenticated:
        messages.info(request, 'Account is already authenticated.')
        return redirect('accounts:telegram_account_detail', pk=pk)

    # Determine current step based on session state
    auth_step = request.session.get(SESSION_KEY_AUTH_STEP.format(pk), 'send_code')

    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'resend_code':
            # Reset to send_code step and resend
            request.session[SESSION_KEY_AUTH_STEP.format(pk)] = 'send_code'
            return _handle_send_code(request, account)

        elif action == 'send_code' or auth_step == 'send_code':
            return _handle_send_code(request, account)

        elif action == 'verify_code' or auth_step == 'verify_code':
            return _handle_verify_code(request, account)

        elif action == 'verify_password' or auth_step == 'verify_password':
            return _handle_verify_password(request, account)

    # GET request - show appropriate form
    context = {
        'account': account,
        'auth_step': auth_step,
    }

    if auth_step == 'verify_code':
        context['form'] = TelegramCodeForm()
    elif auth_step == 'verify_password':
        context['form'] = TelegramPasswordForm()

    return render(request, 'accounts/telegram_account_auth.html', context)


def _handle_send_code(request, account):
    """Handle sending verification code."""
    try:
        service = TelegramService(account)

        async def send_code():
            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number
            )
            result = await service.send_code(account.phone_number)
            # Save session string BEFORE disconnect to preserve auth state
            session_string = service._client.session.save() if service._client else None
            await service.disconnect()
            return result, session_string

        result, session_string = run_async(send_code())

        if result['success']:
            # Save session string synchronously to ensure it's persisted
            if session_string:
                account.session_string = session_string
                account.save(update_fields=['session_string'])
                logger.debug(f"Session saved for account {account.pk}")
                # Also store in Django session as backup
                request.session[SESSION_KEY_SESSION_STRING.format(account.pk)] = session_string
            # Store phone_code_hash in session
            request.session[SESSION_KEY_PHONE_HASH.format(account.pk)] = result['phone_code_hash']
            request.session[SESSION_KEY_AUTH_STEP.format(account.pk)] = 'verify_code'
            request.session.modified = True  # Force session save
            messages.success(request, 'Verification code sent to your phone.')
        else:
            messages.error(request, result.get('error', 'Failed to send code.'))

    except Exception as e:
        logger.exception("Error in send_code")
        messages.error(request, f'Error: {str(e)}')

    return redirect('accounts:telegram_account_auth', pk=account.pk)


def _handle_verify_code(request, account):
    """Handle verification code submission."""
    form = TelegramCodeForm(request.POST)

    if not form.is_valid():
        messages.error(request, 'Please enter a valid code.')
        return redirect('accounts:telegram_account_auth', pk=account.pk)

    code = form.cleaned_data['code']
    phone_code_hash = request.session.get(SESSION_KEY_PHONE_HASH.format(account.pk))

    logger.debug(f"Verifying code for account {account.pk}")

    try:
        # Refresh account and apply session fallback if needed
        account = _get_session_with_fallback(request, account)

        service = TelegramService(account)

        async def verify():
            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number
            )
            result = await service.verify_code(account.phone_number, code, phone_code_hash)
            # Get session string to save
            session_string = service._client.session.save() if service._client else None
            await service.disconnect()
            return result, session_string

        result, session_string = run_async(verify())

        if result['success']:
            # Save session string synchronously
            if session_string:
                account.session_string = session_string
                # Also store in Django session for 2FA step
                request.session[SESSION_KEY_SESSION_STRING.format(account.pk)] = session_string
                request.session.modified = True
            if result.get('requires_2fa'):
                request.session[SESSION_KEY_AUTH_STEP.format(account.pk)] = 'verify_password'
                account.two_factor_enabled = True
                account.save()
                logger.debug(f"2FA required for account {account.pk}")
                messages.info(request, 'Please enter your 2FA password.')
            else:
                # Authentication complete
                account.is_authenticated = True
                account.save()
                _cleanup_auth_session(request, account.pk)
                messages.success(request, 'Account authenticated successfully!')
                return redirect('accounts:telegram_account_detail', pk=account.pk)
        else:
            messages.error(request, result.get('error', 'Invalid code.'))

    except Exception as e:
        logger.exception("Error in verify_code")
        messages.error(request, f'Error: {str(e)}')

    return redirect('accounts:telegram_account_auth', pk=account.pk)


def _handle_verify_password(request, account):
    """Handle 2FA password submission."""
    form = TelegramPasswordForm(request.POST)

    if not form.is_valid():
        messages.error(request, 'Please enter your password.')
        return redirect('accounts:telegram_account_auth', pk=account.pk)

    password = form.cleaned_data['password']
    logger.debug(f"Verifying 2FA password for account {account.pk}")

    try:
        # Refresh account and apply session fallback if needed
        account = _get_session_with_fallback(request, account)

        service = TelegramService(account)

        async def verify():
            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number
            )
            result = await service.verify_password(password)
            # Get session string to save
            session_string = service._client.session.save() if service._client else None
            await service.disconnect()
            return result, session_string

        result, session_string = run_async(verify())

        if result['success']:
            # Save session string synchronously
            if session_string:
                account.session_string = session_string
            account.is_authenticated = True
            account.save()
            _cleanup_auth_session(request, account.pk)
            messages.success(request, 'Account authenticated successfully!')
            return redirect('accounts:telegram_account_detail', pk=account.pk)
        else:
            messages.error(request, result.get('error', 'Invalid password.'))

    except Exception as e:
        logger.exception("Error in verify_password")
        messages.error(request, f'Error: {str(e)}')

    return redirect('accounts:telegram_account_auth', pk=account.pk)


def _cleanup_auth_session(request, account_pk):
    """Clean up authentication session data."""
    keys_to_remove = [
        SESSION_KEY_AUTH_STEP.format(account_pk),
        SESSION_KEY_PHONE_HASH.format(account_pk),
        SESSION_KEY_SESSION_STRING.format(account_pk),
    ]
    for key in keys_to_remove:
        request.session.pop(key, None)


@login_required
@require_http_methods(['POST'])
def telegram_account_deactivate(request, pk):
    """Deactivate a Telegram account (soft delete)."""
    account = get_object_or_404(TelegramAccount, pk=pk)
    account.is_active = False
    account.save(update_fields=['is_active'])
    messages.success(request, f'Account {account.name} deactivated.')
    return redirect('accounts:telegram_accounts')


# Keep old name for backwards compatibility with URL routing
telegram_account_delete = telegram_account_deactivate


@login_required
@require_http_methods(['POST'])
def telegram_account_sync(request, pk):
    """Queue a sync of channels/groups from a Telegram account."""
    account = get_object_or_404(TelegramAccount, pk=pk)
    is_htmx = request.headers.get('HX-Request') == 'true'

    if not account.is_authenticated:
        if is_htmx:
            return _json_response(False, error='Account is not authenticated.')
        messages.error(request, 'Account is not authenticated.')
        return redirect('accounts:telegram_account_detail', pk=pk)

    # Check for existing running sync task
    existing_task = TaskRun.objects.filter(
        task_type='sync_channels',
        account=account,
        status__in=['queued', 'running']
    ).first()

    if existing_task:
        if is_htmx:
            return _json_response(
                False,
                error='Channel sync already in progress.',
                data={'duplicate': True, 'task_id': existing_task.task_id, 'progress': existing_task.progress_percent},
                status=409
            )
        messages.warning(request, 'Channel sync already in progress.')
        return redirect('accounts:telegram_account_detail', pk=pk)

    # Create TaskRun and dispatch the task with atomic dispatch
    try:
        task_run, task_id = dispatch_task(
            sync_account_channels,
            'sync_channels',
            account=account,
            args=(account.pk,),
        )
    except Exception as e:
        if is_htmx:
            return _json_response(False, error=f'Failed to queue task: {e}', status=500)
        messages.error(request, f'Failed to queue channel sync: {e}')
        return redirect('accounts:telegram_account_detail', pk=pk)

    msg = 'Channel sync queued. This may take a moment.'

    if is_htmx:
        return _json_response(True, message=msg, data={'task_id': task_id})

    messages.success(request, msg)
    return redirect('accounts:telegram_account_detail', pk=pk)


@login_required
def telegram_account_listeners(request, pk):
    """Manage listeners for a Telegram account."""
    account = get_object_or_404(TelegramAccount, pk=pk)
    return render(request, 'accounts/telegram_account_listeners.html', {
        'account': account,
    })


@login_required
@require_http_methods(['POST'])
def telegram_listener_start(request, pk):
    """Start the listener for an account."""
    account = get_object_or_404(TelegramAccount, pk=pk)

    if not account.is_authenticated:
        return _json_response(False, error='Account not authenticated', status=400)

    start_listener(account.pk)

    account.listener_status = 'running'
    account.save()

    return _json_response(True, message='Listener started', data={'status': 'started'})


@login_required
@require_http_methods(['POST'])
def telegram_listener_stop(request, pk):
    """Stop the listener for an account."""
    account = get_object_or_404(TelegramAccount, pk=pk)

    stop_listener(account.pk)

    account.listener_status = 'stopped'
    account.save()

    return _json_response(True, message='Listener stopped', data={'status': 'stopped'})


@login_required
def settings(request):
    """Global settings page."""
    settings_obj = GlobalSettings.get_settings()

    if request.method == 'POST':
        form_section = request.POST.get('form_section', 'general')

        # Define which fields belong to which section
        section_fields = {
            'general': [
                'storage_provider', 'storage_root', 'filename_format', 'default_retry_count',
                's3_endpoint_url', 's3_access_key_id', 's3_secret_access_key', 's3_bucket_name',
                's3_region', 's3_presigned_url_expiry',
                'azure_connection_string', 'azure_container_name', 'azure_sas_expiry',
                'run_onboarding_for_new_sources', 'auto_archive_unavailable',
                'auto_discover_sources',
            ],
            'scheduler': ['download_queue_interval', 'channel_sync_interval', 'channel_stats_interval', 'media_counts_interval', 'stuck_task_recovery_interval', 'availability_check_interval', 'forum_topics_sync_interval', 'member_sync_interval', 'reaction_scan_interval'],
            'events': ['dialog_cache_limit', 'event_processing_enabled', 'event_processor_batch_size', 'event_processor_retry_count',
                       'event_processor_retry_backoff_min', 'event_processor_retry_backoff_max', 'tglink_resolution', 'store_raw_events', 'stream_raw_events'],
        }

        # Boolean fields that need string-to-bool conversion
        boolean_fields = {'event_processing_enabled', 'tglink_resolution', 'store_raw_events', 'stream_raw_events', 'run_onboarding_for_new_sources', 'auto_archive_unavailable', 'auto_discover_sources'}

        # Integer fields that need string-to-int conversion
        integer_fields = {
            's3_presigned_url_expiry', 'azure_sas_expiry',
            'default_retry_count', 'dialog_cache_limit',
            'event_processor_batch_size', 'event_processor_retry_count',
            'event_processor_retry_backoff_min', 'event_processor_retry_backoff_max',
            'download_queue_interval', 'channel_sync_interval', 'channel_stats_interval',
            'media_counts_interval', 'stuck_task_recovery_interval', 'availability_check_interval',
            'forum_topics_sync_interval', 'member_sync_interval', 'reaction_scan_interval',
        }

        fields_to_update = section_fields.get(form_section, [])
        redirect_hash = '' if form_section == 'general' else f'#{form_section}'

        # Update only the fields for this section
        for field in fields_to_update:
            if field in request.POST:
                value = request.POST[field]
                # Handle boolean fields sent as "true"/"false" strings
                if field in boolean_fields:
                    value = (value.lower() == 'true')
                # Handle integer fields
                elif field in integer_fields:
                    try:
                        value = int(value)
                    except (ValueError, TypeError):
                        continue
                # Validate storage_root only when using local storage
                if field == 'storage_root' and request.POST.get('storage_provider', 'local') == 'local':
                    resolved = os.path.realpath(value)
                    if not os.path.isabs(resolved):
                        messages.error(request, 'Storage root must be an absolute path.')
                        return redirect(f"{request.path}{redirect_hash}")
                    if not os.path.isdir(resolved):
                        messages.error(request, 'Storage root must be an existing directory.')
                        return redirect(f"{request.path}{redirect_hash}")
                    value = resolved
                setattr(settings_obj, field, value)

        settings_obj.save()
        messages.success(request, 'Settings saved successfully.')
        return redirect(f"{request.path}{redirect_hash}")

    # Get exclusion rules for the exclusions tab with pagination
    exclusions_qs = ExclusionRule.objects.select_related(
        'telegram_user', 'source'
    ).order_by('-created_at')

    exclusions_page = request.GET.get('exclusions_page', 1)
    exclusions_paginator = Paginator(exclusions_qs, 25)
    exclusions = exclusions_paginator.get_page(exclusions_page)

    # Get API tokens for the current user
    tokens = Token.objects.filter(user=request.user)

    return render(request, 'accounts/settings.html', {
        'settings': settings_obj,
        'exclusions': exclusions,
        'tokens': tokens,
    })


@login_required
@require_http_methods(['POST'])
def clear_download_queue(request):
    """Clear all items from the download queue."""
    status_filter = request.POST.get('status', 'all')

    # Build the queryset based on filter
    if status_filter == 'all':
        qs = DownloadTask.objects.exclude(status='completed')
    elif status_filter == 'failed':
        qs = DownloadTask.objects.filter(status='failed')
    elif status_filter == 'pending':
        qs = DownloadTask.objects.filter(status='pending')
    elif status_filter == 'paused':
        qs = DownloadTask.objects.filter(status='paused')
    else:
        qs = DownloadTask.objects.none()

    # Delete in batches to avoid timeout on large queues
    # Using _raw_delete bypasses signals/history for performance
    deleted = 0
    batch_size = 1000
    while True:
        # Get batch of IDs to delete
        batch_ids = list(qs.values_list('id', flat=True)[:batch_size])
        if not batch_ids:
            break
        # Delete this batch
        count, _ = DownloadTask.objects.filter(id__in=batch_ids).delete()
        deleted += count

    if deleted:
        messages.success(request, f'Deleted {deleted} items from the download queue.')
    else:
        messages.info(request, 'No items to delete.')

    return redirect(reverse('downloads:index') + '?tab=queue')


@login_required
@require_http_methods(['POST'])
def clear_task_runs(request):
    """Clear task runs based on status filter."""
    status_filter = request.POST.get('status', 'all')

    if status_filter == 'all':
        # Delete all non-running tasks
        deleted, _ = TaskRun.objects.exclude(status='running').delete()
    elif status_filter == 'queued':
        deleted, _ = TaskRun.objects.filter(status='queued').delete()
    elif status_filter == 'failed':
        deleted, _ = TaskRun.objects.filter(status='failed').delete()
    elif status_filter == 'completed':
        deleted, _ = TaskRun.objects.filter(status='completed').delete()
    elif status_filter == 'cancelled':
        deleted, _ = TaskRun.objects.filter(status='cancelled').delete()
    else:
        deleted = 0

    if deleted:
        messages.success(request, f'Deleted {deleted} task(s).')
    else:
        messages.info(request, 'No tasks to delete.')

    return redirect('audit:tasks')


@login_required
@require_http_methods(['POST'])
def cancel_task_run(request, pk):
    """Request cancellation for a running task."""
    # Get task with ownership verification
    task = get_object_or_404(
        TaskRun.objects.filter(
            Q(channel__account__user=request.user) | Q(account__user=request.user)
        ),
        pk=pk
    )
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    force = request.GET.get('force') == '1'

    if task.status not in ['queued', 'running']:
        if is_ajax:
            return _json_response(False, error=f'Cannot cancel task with status: {task.status}', status=400)
        messages.info(request, 'Task is not running.')
        return redirect('audit:tasks')

    if force:
        # Force cancel - immediately mark as cancelled
        task.mark_cancelled()
        if is_ajax:
            return _json_response(True, message='Task force cancelled.', data={'status': 'cancelled'})
        messages.success(request, 'Task force cancelled.')
    else:
        # Normal cancel - request cooperative cancellation
        task.request_cancel()
        if is_ajax:
            return _json_response(True, message='Cancellation requested. Task will stop at next checkpoint.', data={'status': 'cancelling'})
        messages.success(request, f'Cancellation requested for task {task.task_id[:8]}...')

    return redirect('audit:tasks')


@login_required
@require_http_methods(['POST'])
def telegram_account_join_channel(request, pk):
    """Join a channel or group by URL."""
    account = get_object_or_404(TelegramAccount, pk=pk)

    if not account.is_authenticated:
        messages.error(request, 'Account is not authenticated.')
        return redirect('accounts:telegram_account_detail', pk=pk)

    url = request.POST.get('channel_url', '').strip()
    if not url:
        messages.error(request, 'Please enter a channel URL or username.')
        return redirect('accounts:telegram_account_detail', pk=pk)

    try:
        service = TelegramService(account)

        async def do_join():
            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number
            )
            result = await service.join_channel(url)

            # If successful, sync dialogs to get the new channel
            if result['success']:
                dialog_limit = GlobalSettings.get_settings().dialog_cache_limit
                dialogs = await service.get_dialogs(limit=dialog_limit)
                await service.disconnect()
                return result, dialogs
            else:
                await service.disconnect()
                return result, None

        result, dialogs = run_async(do_join())

        if result['success']:
            if dialogs:
                _sync_dialogs_to_db(account, dialogs)
            messages.success(request, f"Successfully joined: {result.get('title', 'channel')}")
        else:
            messages.error(request, result.get('error', 'Failed to join channel'))

    except Exception as e:
        logger.exception("Error joining channel")
        messages.error(request, f'Error: {str(e)}')

    return redirect('accounts:telegram_account_detail', pk=pk)


@login_required
@require_http_methods(['POST'])
def join_channel(request):
    """Join a channel or group - generic endpoint that accepts account_id in POST."""
    account_id = request.POST.get('account_id')
    url = request.POST.get('channel_url', '').strip()
    # Checkbox is present in POST only when checked, default to True for backwards compatibility
    run_onboarding = request.POST.get('run_onboarding') == 'on'

    # Validate redirect URL to prevent open redirect attacks
    redirect_url = request.POST.get('next') or request.META.get('HTTP_REFERER')
    if not redirect_url or not url_has_allowed_host_and_scheme(redirect_url, allowed_hosts={request.get_host()}):
        redirect_url = 'dashboard:index'

    if not account_id:
        messages.error(request, 'Please select an account.')
        return redirect(redirect_url)

    account = get_object_or_404(TelegramAccount, pk=account_id)

    if not account.is_authenticated:
        messages.error(request, 'Selected account is not authenticated.')
        return redirect(redirect_url)

    if not url:
        messages.error(request, 'Please enter a channel URL or username.')
        return redirect(redirect_url)

    try:
        service = TelegramService(account)

        async def do_join():
            await service.create_client(
                account.api_id,
                account.api_hash,
                account.phone_number
            )
            result = await service.join_channel(url)

            # If successful, sync dialogs to get the new channel
            if result['success']:
                dialog_limit = GlobalSettings.get_settings().dialog_cache_limit
                dialogs = await service.get_dialogs(limit=dialog_limit)
                await service.disconnect()
                return result, dialogs
            else:
                await service.disconnect()
                return result, None

        result, dialogs = run_async(do_join())

        if result['success']:
            if dialogs:
                _sync_dialogs_to_db(account, dialogs, run_onboarding=run_onboarding)
            messages.success(request, f"Successfully joined: {result.get('title', 'channel')}")
        else:
            messages.error(request, result.get('error', 'Failed to join channel'))

    except Exception as e:
        logger.exception("Error joining channel")
        messages.error(request, f'Error: {str(e)}')

    return redirect(redirect_url)


# ============================================================================
# Exclusion List Views
# ============================================================================

@login_required
def exclusion_list(request):
    """Return exclusion rules table as HTMX partial or full page section."""
    exclusions = ExclusionRule.objects.select_related(
        'telegram_user', 'source'
    ).order_by('-created_at')

    return render(request, 'accounts/partials/exclusion_table.html', {
        'exclusions': exclusions,
    })


@login_required
@require_http_methods(['POST'])
def exclusion_add(request):
    """Add new exclusion rule(s)."""
    user_id = request.POST.get('user_id')
    exclusion_type = request.POST.get('exclusion_type', 'global')
    source_ids = request.POST.getlist('source_ids')
    reason = request.POST.get('reason', '').strip()

    if not user_id:
        return _json_response(False, error='User is required', status=400)

    try:
        telegram_user = TelegramUser.objects.get(pk=user_id)
    except TelegramUser.DoesNotExist:
        return _json_response(False, error='User not found', status=404)

    created_count = 0
    errors = []

    if exclusion_type == 'global':
        # Create global exclusion
        try:
            ExclusionRule.objects.create(
                telegram_user=telegram_user,
                source=None,
                is_global=True,
                reason=reason,
            )
            created_count = 1
        except IntegrityError:
            errors.append(f'Global exclusion already exists for {telegram_user}')
    else:
        # Create source-specific exclusions
        if not source_ids:
            return _json_response(False, error='At least one source is required', status=400)

        for source_id in source_ids:
            try:
                source = TelegramChannel.objects.get(pk=source_id)
                ExclusionRule.objects.create(
                    telegram_user=telegram_user,
                    source=source,
                    is_global=False,
                    reason=reason,
                )
                created_count += 1
            except TelegramChannel.DoesNotExist:
                errors.append(f'Source {source_id} not found')
            except IntegrityError:
                errors.append(f'Exclusion already exists for {telegram_user} in source {source_id}')

    if created_count > 0:
        return _json_response(
            True,
            message=f'Created {created_count} exclusion(s)',
            data={'errors': errors} if errors else None
        )
    else:
        return _json_response(
            False,
            error=errors[0] if errors else 'Failed to create exclusion',
            data={'errors': errors},
            status=400
        )


@login_required
@require_http_methods(['POST'])
def exclusion_toggle(request, pk):
    """Toggle exclusion rule active/inactive."""
    try:
        exclusion = ExclusionRule.objects.get(pk=pk)
    except ExclusionRule.DoesNotExist:
        return _json_response(False, error='Exclusion not found', status=404)

    exclusion.is_active = not exclusion.is_active
    exclusion.save(update_fields=['is_active', 'updated_at'])

    return _json_response(
        True,
        message=f'Exclusion {"enabled" if exclusion.is_active else "disabled"}',
        data={'is_active': exclusion.is_active}
    )


@login_required
@require_http_methods(['POST'])
def exclusion_delete(request, pk):
    """Delete an exclusion rule."""
    try:
        exclusion = ExclusionRule.objects.get(pk=pk)
    except ExclusionRule.DoesNotExist:
        return _json_response(False, error='Exclusion not found', status=404)

    exclusion.delete()

    return _json_response(True, message='Exclusion deleted')


@login_required
def search_users_api(request):
    """Search for Telegram users by username, telegram_id, or pk."""
    query = request.GET.get('q', '').strip()
    if not query or len(query) < 2:
        return _json_response(True, data={'results': []})

    # Build search filter
    filters = Q(username__icontains=query) | Q(first_name__icontains=query) | Q(last_name__icontains=query)

    # Try numeric search for telegram_id or pk
    if query.isdigit():
        filters |= Q(telegram_id=query) | Q(pk=query)

    users = TelegramUser.objects.filter(filters)[:20]

    results = []
    for user in users:
        display_name = user.display_name
        results.append({
            'id': user.pk,
            'text': display_name,
            'telegram_id': user.telegram_id,
            'username': user.username,
        })

    return _json_response(True, data={'results': results})


@login_required
def search_sources_api(request):
    """Search for sources by title, username, telegram_id, or pk."""
    query = request.GET.get('q', '').strip()
    if not query or len(query) < 2:
        return _json_response(True, data={'results': []})

    # Build search filter - only sources accessible to current user
    filters = Q(title__icontains=query) | Q(username__icontains=query)

    # Try numeric search for telegram_id or pk
    if query.isdigit():
        filters |= Q(telegram_id=query) | Q(pk=query)

    sources = TelegramChannel.objects.filter(
        filters,
    ).select_related('account')[:20]

    results = []
    for source in sources:
        display_name = source.display_name
        results.append({
            'id': source.pk,
            'text': display_name,
            'telegram_id': source.telegram_id,
            'username': source.username,
        })

    return _json_response(True, data={'results': results})


# ============================================================================
# Manual Task Invocation Views
# ============================================================================

@login_required
@require_http_methods(['POST'])
def invoke_process_downloads(request):
    """Manually trigger the process download queue task."""
    process_download_queue.send()
    return _json_response(True, message='Download queue processing triggered')


@login_required
@require_http_methods(['POST'])
def invoke_sync_channels(request):
    """Manually trigger the sync all account channels task."""
    sync_all_account_channels.send()
    return _json_response(True, message='Channel sync triggered for all accounts')


@login_required
@require_http_methods(['POST'])
def invoke_refresh_stats(request):
    """Manually trigger the refresh all channel stats task."""
    refresh_all_channel_stats.send()
    return _json_response(True, message='Channel stats refresh triggered')


@login_required
@require_http_methods(['POST'])
def invoke_refresh_media_counts(request):
    """Manually trigger the refresh all media counts task."""
    refresh_all_media_counts.send()
    return _json_response(True, message='Media counts refresh triggered')


@login_required
@require_http_methods(['POST'])
def invoke_recover_stuck_tasks(request):
    """Manually trigger stuck task recovery."""
    result = recover_stuck_tasks()
    return _json_response(
        True,
        message=f"Recovery complete: {result['task_runs']} task runs failed, {result['download_tasks']} downloads reset",
        data={'details': result}
    )


@login_required
@require_http_methods(['POST'])
def invoke_check_availability(request):
    """Manually trigger availability check for all channels."""
    check_all_source_availability.send()
    return _json_response(True, message='Availability check triggered for all channels')


@login_required
@require_http_methods(['POST'])
def invoke_sync_forum_topics(request):
    """Manually trigger forum topics sync for all forum channels."""
    sync_all_forum_topics.send()
    return _json_response(True, message='Forum topics sync triggered for all forum channels')


@login_required
@require_http_methods(['GET'])
def get_dead_letters_stats(request):
    """Get counts of messages in dead letter queues."""
    stats = get_dead_letter_stats()
    return _json_response(True, data=stats)


@login_required
@require_http_methods(['POST'])
def invoke_requeue_dead_letters(request):
    """Requeue all dead letter messages back to their original queues."""
    result = requeue_dead_letters()
    if result.get('success'):
        total = result.get('total', 0)
        return _json_response(True, message=f'Requeued {total} dead letter message(s)')
    return _json_response(False, error=result.get('error', 'Unknown error'))


@login_required
@require_http_methods(['POST'])
def invoke_purge_dead_letters(request):
    """Purge all dead letter messages (discard without retrying)."""
    result = purge_dead_letters()
    if result.get('success'):
        total = result.get('total', 0)
        return _json_response(True, message=f'Purged {total} dead letter message(s)')
    return _json_response(False, error=result.get('error', 'Unknown error'))


# ============================================================================
# API Token Management Views
# ============================================================================

@login_required
@require_POST
def api_token_create(request):
    """Create a new API token for the user."""
    # Check if user already has a token
    existing_token = Token.objects.filter(user=request.user).first()
    if existing_token:
        return _json_response(
            False,
            error='You already have an API token. Delete it first to create a new one.',
            status=400
        )

    # Create new token
    token = Token.objects.create(user=request.user)

    return _json_response(
        True,
        message='API token created successfully.',
        data={'token': token.key}
    )


@login_required
@require_POST
def api_token_regenerate(request):
    """Regenerate the user's API token."""
    # Delete existing token if any
    Token.objects.filter(user=request.user).delete()

    # Create new token
    token = Token.objects.create(user=request.user)

    return _json_response(
        True,
        message='API token regenerated successfully.',
        data={'token': token.key}
    )


@login_required
@require_POST
def api_token_delete(request):
    """Delete the user's API token."""
    deleted_count, _ = Token.objects.filter(user=request.user).delete()

    if deleted_count > 0:
        return _json_response(True, message='API token deleted successfully.')
    else:
        return _json_response(False, error='No API token found to delete.', status=404)
