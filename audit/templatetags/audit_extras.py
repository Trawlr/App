"""Custom template tags and filters for the audit app."""

import re
from django import template
from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.timesince import timesince

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """Get an item from a dictionary using a variable key."""
    if dictionary is None:
        return None
    return dictionary.get(key)


@register.filter
def get_message_text(archived_messages, message_id):
    """Get the text from an archived message by message_id."""
    if archived_messages is None:
        return ''
    message = archived_messages.get(message_id)
    if message:
        return message.text
    return ''


@register.filter
def display_title(channel):
    """Display channel title, falling back to telegram_id if title is blank/invisible.

    Handles invisible Unicode characters like Hangul Filler (ㅤ) that render as blank
    but aren't technically whitespace.
    """
    title = getattr(channel, 'title', '')

    if not title:
        return getattr(channel, 'telegram_id', str(channel.pk))

    # Known invisible characters not caught by strip()
    invisible_chars = '\u3164\u2800\u200B\u200C\u200D\uFEFF\u00A0\u2063'
    visible = title.strip()
    for char in invisible_chars:
        visible = visible.replace(char, '')

    if not visible:
        return getattr(channel, 'telegram_id', str(channel.pk))

    return title


@register.filter
def post_datetime(value):
    """Format datetime as 'Mon 19 Jan 2026 13:30:01 UTC (2 hours ago)'.

    Returns formatted string with day name, date, time in UTC and relative time.
    """
    if not value:
        return ''

    # Format: Day Date Month Year HH:mm:ss UTC
    formatted = value.strftime('%a %d %b %Y %H:%M:%S UTC')

    # Add relative time
    relative = timesince(value, timezone.now())
    return f"{formatted} ({relative} ago)"


@register.filter
def post_datetime_short(value):
    """Format datetime as 'Mon 19 Jan 2026 13:30:01 UTC' without relative time."""
    if not value:
        return ''
    return value.strftime('%a %d %b %Y %H:%M:%S UTC')


@register.filter
def format_ttl(seconds):
    """Format TTL seconds into human-readable format (e.g., 30s, 5m, 1h, 1d)."""
    if not seconds:
        return ''

    try:
        seconds = int(seconds)
    except (ValueError, TypeError):
        return str(seconds)

    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        return f"{minutes}m"
    elif seconds < 86400:
        hours = seconds // 3600
        return f"{hours}h"
    else:
        days = seconds // 86400
        return f"{days}d"


@register.filter
def intcomma_short(value):
    """Format numbers with K/M suffixes: 1234 -> 1.2K, 1234567 -> 1.2M."""
    try:
        value = int(value)
    except (ValueError, TypeError):
        return value or ''

    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    elif value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


@register.filter
def highlight_search(text, query):
    """Highlight search terms in text."""
    if not text or not query:
        return escape(text) if text else ''

    # Extract text search terms from query
    terms = []

    # Match text:term patterns
    for match in re.finditer(r'text:(?:"([^"]+)"|(\S+))', query, re.IGNORECASE):
        term = match.group(1) or match.group(2)
        if term:
            terms.append(term)

    # Also include bare words (not field:value patterns)
    for word in query.split():
        if ':' not in word and word.upper() not in ('AND', 'OR', 'NOT'):
            terms.append(word.strip('"'))

    if not terms:
        return escape(text)

    # Escape the text first
    result = escape(text)

    # Highlight each term
    for term in terms:
        if len(term) < 2:
            continue
        try:
            pattern = re.compile(re.escape(term), re.IGNORECASE)
            result = pattern.sub(lambda m: f'<mark>{m.group()}</mark>', result)
        except re.error:
            continue

    return mark_safe(result)
