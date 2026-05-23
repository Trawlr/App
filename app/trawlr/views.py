import logging

from django.http import HttpResponseForbidden

logger = logging.getLogger('trawlr')


def csrf_failure(request, reason=""):
    """Custom CSRF failure view that logs diagnostic info."""
    origin = request.META.get('HTTP_ORIGIN', 'not set')
    referer = request.META.get('HTTP_REFERER', 'not set')
    host = request.META.get('HTTP_HOST', 'not set')
    forwarded_proto = request.META.get('HTTP_X_FORWARDED_PROTO', 'not set')
    is_secure = request.is_secure()

    logger.error(
        "CSRF failure: %s | Origin: %s | Referer: %s | Host: %s | "
        "X-Forwarded-Proto: %s | is_secure: %s",
        reason, origin, referer, host, forwarded_proto, is_secure,
    )

    from django.conf import settings
    trusted = getattr(settings, 'CSRF_TRUSTED_ORIGINS', [])

    return HttpResponseForbidden(
        f"<h1>CSRF Verification Failed</h1>"
        f"<p><b>Reason:</b> {reason}</p>"
        f"<p><b>Origin:</b> {origin}</p>"
        f"<p><b>Referer:</b> {referer}</p>"
        f"<p><b>Host:</b> {host}</p>"
        f"<p><b>X-Forwarded-Proto:</b> {forwarded_proto}</p>"
        f"<p><b>is_secure():</b> {is_secure}</p>"
        f"<p><b>CSRF_TRUSTED_ORIGINS:</b> {trusted}</p>",
        content_type="text/html",
    )
