"""
Domain parser - extracts domain names from URLs in messages.

Creates 'domain' entities for each unique domain found in URL entities.
"""
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .base import BaseParser

if TYPE_CHECKING:
    from typing import Any


class DomainParser(BaseParser):
    """
    Extracts domain names from URL entities.

    For each URL entity (type 'url' or 'text_url'), creates a 'domain' entity
    containing just the domain portion (e.g., 't.me', 'example.com').
    """

    @property
    def name(self) -> str:
        return "domain"

    def parse(self, payload: 'dict[str, Any]', entities: 'list[dict]') -> 'list[dict]':
        """Extract domains from URL entities."""
        domain_entities = []
        seen_domains: set[str] = set()

        for entity in entities:
            entity_type = entity.get('entity_type', '')

            # Only process URL-type entities
            if entity_type not in ('url', 'text_url'):
                continue

            # Get the URL (from 'url' field for text_url, or 'text' for plain url)
            url = entity.get('url') or entity.get('text', '')
            if not url:
                continue

            domain = self._extract_domain(url)
            if not domain or domain in seen_domains:
                continue

            seen_domains.add(domain)

            domain_entities.append({
                'entity_type': 'domain',
                'offset': entity.get('offset', 0),
                'length': len(domain),
                'text': domain,
                'url': url,  # Store the source URL
                'user_id': None,
                'language': '',
                'custom_emoji_id': None,
            })

        return domain_entities

    def _extract_domain(self, url: str) -> str:
        """
        Extract domain from a URL string.

        Handles URLs with or without scheme prefix.
        Returns just the hostname without port.
        """
        # Clean markdown formatting that may have leaked into URL
        # (happens when bold ** is adjacent to URL without space)
        url = url.strip()
        url = url.lstrip('*_~`')  # Strip leading markdown chars
        url = url.rstrip('*_~`')  # Strip trailing markdown chars

        # Add scheme if missing (urlparse needs it)
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        try:
            parsed = urlparse(url)
            hostname = parsed.hostname or ''
            # Remove 'www.' prefix if present
            if hostname.startswith('www.'):
                hostname = hostname[4:]
            # Validate: must contain a dot and no invalid chars
            if '.' not in hostname or ' ' in hostname or '*' in hostname:
                return ''
            return hostname.lower()
        except Exception:
            return ''
