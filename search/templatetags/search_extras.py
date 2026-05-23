"""Template tags for search functionality."""

import re
from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()

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


@register.filter
def get_item(dictionary, key):
    """Get item from dictionary by key."""
    if dictionary is None:
        return None
    return dictionary.get(key)
