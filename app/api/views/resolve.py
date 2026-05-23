"""
Async view for resolving t.me links via Telegram API.

Uses a plain Django async view (not DRF APIView) because DRF 3.x does not
support async handler methods on APIView. CSRF is exempt since we validate
the session manually; the X-CSRFToken header is still checked by the
csrf_protect decorator equivalent in the frontend.
"""

import json
import logging

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from accounts.models import TelegramAccount
from accounts.telegram_service import TelegramService
from audit.models import TelegramLink, TelegramChannel, TelegramUser
from parsers.tme_parser import parse_tme_link

logger = logging.getLogger('trawlr.api')


@csrf_exempt
@require_POST
async def resolve_link(request):
    """Resolve a t.me link and return entity information."""
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Authentication required'}, status=401)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)

    url = body.get('url', '').strip()
    if not url:
        return JsonResponse({'error': 'url is required'}, status=400)

    # Parse the t.me link
    parsed = parse_tme_link(url)
    if not parsed:
        return JsonResponse({'error': 'Not a valid t.me link'}, status=400)

    # Check if we already have this link resolved
    existing = await TelegramLink.objects.filter(url=parsed['url']).afirst()
    if existing and existing.status == 'resolved':
        return JsonResponse({
            'status': 'resolved',
            'link_type': existing.link_type,
            'data': existing.raw_data,
            'resolved_at': existing.resolved_at.isoformat() if existing.resolved_at else None,
            'cached': True,
        })

    # Pick account: use specified account_id or fall back to first authenticated
    account_id = body.get('account_id')
    if account_id:
        account = await TelegramAccount.objects.filter(
            pk=account_id, is_active=True, is_authenticated=True
        ).afirst()
    else:
        account = await TelegramAccount.objects.authenticated().afirst()
    if not account:
        return JsonResponse({'error': 'No authenticated Telegram account available'}, status=503)

    # Resolve via Telegram API
    service = TelegramService(account)
    try:
        await service.create_client(account.api_id, account.api_hash, account.phone_number)
        result = await service.resolve_entity(parsed['identifier'], parsed['link_type'])
    finally:
        await service.disconnect()

    # Create or update the TelegramLink record
    link_defaults = {
        'identifier': parsed['identifier'],
    }

    if result['success']:
        entity_type = result['entity_type']
        data = result['data']
        link_defaults.update({
            'link_type': entity_type,
            'status': 'resolved',
            'raw_data': data,
            'resolved_at': timezone.now(),
            'error': '',
        })

        # Link to existing models if possible
        telegram_id = data.get('id')
        if telegram_id:
            if entity_type == 'channel':
                channel = await TelegramChannel.objects.filter(
                    telegram_id=telegram_id
                ).afirst()
                if channel:
                    link_defaults['resolved_channel'] = channel
            elif entity_type in ('user', 'bot'):
                user = await TelegramUser.objects.filter(
                    telegram_id=telegram_id
                ).afirst()
                if user:
                    link_defaults['resolved_user'] = user

        if entity_type == 'invite':
            link_defaults['invite_title'] = data.get('title', '')
            link_defaults['invite_member_count'] = data.get('participants_count')
    else:
        link_defaults.update({
            'link_type': 'unknown',
            'status': 'failed',
            'error': result.get('error', 'Unknown error'),
        })

    link, created = await TelegramLink.objects.aupdate_or_create(
        url=parsed['url'],
        defaults=link_defaults,
    )

    if result['success']:
        return JsonResponse({
            'status': 'resolved',
            'link_type': result['entity_type'],
            'data': result['data'],
            'resolved_at': link.resolved_at.isoformat() if link.resolved_at else None,
            'cached': False,
        })
    else:
        return JsonResponse({
            'status': 'failed',
            'error': result.get('error', 'Resolution failed'),
        }, status=422)
