"""
t.me link parser - extracts and classifies t.me links from URLs.
"""

import re
from urllib.parse import urlparse


TME_DOMAINS = {'t.me', 'telegram.me', 't.link'}


def parse_tme_link(url: str) -> dict | None:
    """
    Parse a t.me URL and return its type and identifier.

    Returns:
        dict with 'url', 'identifier', 'link_type' keys, or None if not a t.me link.
        link_type is one of: 'invite', 'username'
    """
    url = url.strip()

    # Add scheme if missing
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    try:
        parsed = urlparse(url)
    except Exception:
        return None

    hostname = (parsed.hostname or '').lower()
    if hostname not in TME_DOMAINS:
        return None

    path = parsed.path.strip('/')
    if not path:
        return None

    # Invite links: /+hash or /joinchat/hash
    if path.startswith('+') or path.startswith('joinchat/'):
        if path.startswith('+'):
            invite_hash = path[1:].split('/')[0]
        else:
            invite_hash = path.split('joinchat/')[-1].split('/')[0]
        if invite_hash:
            return {
                'url': url,
                'identifier': invite_hash,
                'link_type': 'invite',
            }
        return None

    # Username links: /username (possibly with /message_id after)
    username = path.split('/')[0]
    # Validate username format (5-32 chars, alphanumeric + underscore)
    if re.match(r'^[a-zA-Z][a-zA-Z0-9_]{3,31}$', username):
        return {
            'url': url,
            'identifier': username,
            'link_type': 'username',
        }

    return None
