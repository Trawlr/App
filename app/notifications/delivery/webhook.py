"""
HTTPS webhook delivery target.

POSTs the JSON payload with an optional HMAC-SHA256 signature header.
Raises error on 4xx/5xx for exponential backoff and retry.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
from urllib.parse import urlparse

import httpx
from django.conf import settings

logger = logging.getLogger('trawlr.notifications.webhook')

REQUEST_TIMEOUT_S = 10.0
ALLOWED_SCHEMES = ('http', 'https')


class SSRFValidationError(ValueError):
    pass

class WebhookDeliveryError(RuntimeError):
    """Raised when a webhook POST returns a non-2xx response or network errors."""

def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    """
    Block only addresses that are unsafe even for self-hosted use.
    Private (RFC 1918) and loopback are intentionally allowed.
    """
    return (
        ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )

def validate_webhook_url(url: str) -> None:
    """
    Reject webhook URLs that target unsafe address ranges.

    All IP-level checks can be bypassed entirely by setting
    settings.NOTIFICATIONS_SSRF_ALLOW_PRIVATE = True
    """
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise SSRFValidationError(f"Unsupported scheme: {parsed.scheme!r}. Use http or https.")
    if not parsed.hostname:
        raise SSRFValidationError("URL has no hostname.")

    if getattr(settings, 'NOTIFICATIONS_SSRF_ALLOW_PRIVATE', False):
        return

    host = parsed.hostname.lower()

    try:
        addrinfo = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == 'https' else 80),
                                      type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise SSRFValidationError(f"DNS lookup failed for {host!r}: {e}")

    for family, _stype, _proto, _canon, sockaddr in addrinfo:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise SSRFValidationError(
                f"Refusing to target {host!r} → {ip_str} "
                "(link-local / cloud-metadata / multicast / reserved range)."
            )


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def deliver_webhook(delivery) -> None:
    """
    Perform the HTTP POST.
    """
    cfg = delivery.entry.target_config or {}
    url = (cfg.get('url') or '').strip()
    secret = (cfg.get('secret') or '').strip()
    extra_headers = cfg.get('headers') or {}

    validate_webhook_url(url)

    body = json.dumps(delivery.event_payload, separators=(',', ':')).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Trawlr-Notifications/1',
        'X-Trawlr-Delivery-Id': str(delivery.pk),
        'X-Trawlr-Entry-Id': str(delivery.entry_id),
        **{str(k): str(v) for k, v in extra_headers.items()},
    }
    if secret:
        headers['X-Trawlr-Signature'] = _sign(secret, body)

    logger.info("Posting webhook delivery=%s entry=%s url=%s", delivery.pk, delivery.entry_id, url)
    try:
        response = httpx.post(
            url,
            content=body,
            headers=headers,
            timeout=REQUEST_TIMEOUT_S,
            follow_redirects=False,
        )
    except httpx.HTTPError as e:
        raise WebhookDeliveryError(f"HTTP error contacting {url}: {e}") from e

    if response.is_error:
        raise WebhookDeliveryError(
            f"Webhook returned {response.status_code} for {url}: {response.text[:200]}"
        )
