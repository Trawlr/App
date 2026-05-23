"""
Filter classes that translate AST nodes into Django Q objects.
"""

import re
from abc import ABC, abstractmethod
from datetime import datetime

from django.db.models import Exists, OuterRef, Q

from audit.models import MessageEntity
from downloads.models import ArchivedMessage

from .parser import (
    AndNode,
    BareQuery,
    FieldQuery,
    OrNode,
    SearchNode,
    parse_query,
    parse_relative_date,
)


class BaseFilter(ABC):
    """Base class for field filters."""

    field_name: str

    @abstractmethod
    def to_q(self, query: FieldQuery) -> Q:
        """Convert field query to Django Q object."""
        pass

    def validate(self, query: FieldQuery) -> list:
        """Return list of warning strings for invalid values. Empty = valid."""
        return []


class TextFilter(BaseFilter):
    """
    Full-text search on message text.
    Uses icontains for partial match, iexact for exact match (quoted values).
    """

    field_name = 'text'

    def to_q(self, query: FieldQuery) -> Q:
        if query.exact:
            # Exact match - text must equal the value exactly (case-insensitive)
            q = Q(text__iexact=query.value)
        else:
            # Partial match - text contains the value
            q = Q(text__icontains=query.value)
        return ~q if query.negated else q


class TextFallbackFilter(BaseFilter):
    """Simple text search using icontains/iexact (fallback when FTS not populated)."""

    field_name = 'text'

    def to_q(self, query: FieldQuery) -> Q:
        if query.exact:
            q = Q(text__iexact=query.value)
        else:
            q = Q(text__icontains=query.value)
        return ~q if query.negated else q


class EntityFilter(BaseFilter):
    """Filter by MessageEntity relationships."""

    entity_type: str
    search_field: str

    def __init__(self, entity_type: str, search_field: str = 'text'):
        self.entity_type = entity_type
        self.search_field = search_field

    def to_q(self, query: FieldQuery) -> Q:
        value = query.value.lstrip('@#$')  # Strip common prefixes

        # Build filter for the entity — search_field is 'text' or 'url' on GlobalEntity
        field = f'entity__{self.search_field}'
        if '*' in value:
            # Convert glob pattern to regex (escape metacharacters first)
            pattern = re.escape(value).replace(r'\*', '.*')
            entity_filter = Q(**{f'{field}__iregex': f'^{pattern}$'})
        elif query.exact:
            # Exact match (quoted value)
            entity_filter = Q(**{f'{field}__iexact': value})
        else:
            # Partial match
            entity_filter = Q(**{f'{field}__icontains': value})

        subquery = MessageEntity.objects.filter(
            message_id=OuterRef('pk'),
            entity__entity_type=self.entity_type,
        ).filter(entity_filter)

        q = Exists(subquery)
        return ~q if query.negated else q


class UrlFilter(BaseFilter):
    """Filter by URL entities."""

    field_name = 'url'

    def to_q(self, query: FieldQuery) -> Q:
        value = query.value

        # Search both 'url' and 'text_url' entity types
        subquery = MessageEntity.objects.filter(
            message_id=OuterRef('pk'),
            entity__entity_type__in=['url', 'text_url'],
        )

        if '*' in value:
            pattern = re.escape(value).replace(r'\*', '.*')
            subquery = subquery.filter(entity__url__iregex=f'.*{pattern}.*')
        elif query.exact:
            # Exact match (quoted value)
            subquery = subquery.filter(entity__url__iexact=value)
        else:
            # Partial match
            subquery = subquery.filter(entity__url__icontains=value)

        q = Exists(subquery)
        return ~q if query.negated else q


class DomainFilter(BaseFilter):
    """Filter by domain entities (extracted domain names from URLs)."""

    field_name = 'domain'

    def to_q(self, query: FieldQuery) -> Q:
        value = query.value

        subquery = MessageEntity.objects.filter(
            message_id=OuterRef('pk'),
            entity__entity_type='domain',
        )

        if '*' in value:
            pattern = re.escape(value).replace(r'\*', '.*')
            subquery = subquery.filter(entity__text__iregex=f'.*{pattern}.*')
        elif query.exact:
            subquery = subquery.filter(entity__text__iexact=value)
        else:
            subquery = subquery.filter(entity__text__icontains=value)

        q = Exists(subquery)
        return ~q if query.negated else q


class MentionFilter(EntityFilter):
    """Filter by mention entities."""

    field_name = 'mention'

    def __init__(self):
        super().__init__('mention', search_field='text')


class HashtagFilter(EntityFilter):
    """Filter by hashtag entities."""

    field_name = 'hashtag'

    def __init__(self):
        super().__init__('hashtag', search_field='text')


class EmailFilter(EntityFilter):
    """Filter by email entities."""

    field_name = 'email'

    def __init__(self):
        super().__init__('email', search_field='text')


class PhoneFilter(EntityFilter):
    """Filter by phone entities."""

    field_name = 'phone'

    def __init__(self):
        super().__init__('phone', search_field='text')


class ChannelFilter(BaseFilter):
    """Filter by channel title, username, Telegram ID, or Trawlr ID."""

    field_name = 'channel'

    def to_q(self, query: FieldQuery) -> Q:
        value = query.value

        # Check if value is numeric (could be Telegram ID or Trawlr ID)
        is_numeric = value.lstrip('-').isdigit()

        if query.exact:
            # Exact match on title/username
            q = Q(channel__title__iexact=value) | Q(channel__username__iexact=value)
        else:
            # Partial match on title/username
            q = Q(channel__title__icontains=value) | Q(channel__username__icontains=value)

        # Add ID match if numeric
        if is_numeric:
            numeric_val = int(value)
            q |= Q(channel__telegram_id=numeric_val) | Q(channel__pk=numeric_val)

        return ~q if query.negated else q


class SenderFilter(BaseFilter):
    """Filter by sender username, name, or Telegram ID."""

    field_name = 'sender'

    def to_q(self, query: FieldQuery) -> Q:
        value = query.value.lstrip('@')

        # Check if value is numeric (could be Telegram user ID)
        is_numeric = value.lstrip('-').isdigit()

        if query.exact:
            # Exact match - username or name must match exactly
            q = (
                Q(sender_username__iexact=value) |
                Q(sender_name__iexact=value)
            )
        else:
            # Partial match
            q = (
                Q(sender_username__icontains=value) |
                Q(sender_name__icontains=value)
            )

        # Add sender_id match if numeric
        if is_numeric:
            numeric_val = int(value)
            q |= Q(sender_id=numeric_val)

        return ~q if query.negated else q


class DateFilter(BaseFilter):
    """
    Filter by created date with relative date support.

    For relative dates (e.g., 5min, 7d), the operator refers to MESSAGE AGE:
      - created<=5min  = "age at most 5 min" = recent messages = timestamp >= now-5min
      - created>=7d    = "age at least 7 days" = older messages = timestamp <= now-7d

    For absolute dates (e.g., 2024-01-15), the operator refers to TIMESTAMP directly:
      - created>=2024-01-15 = timestamp >= 2024-01-15
    """

    field_name = 'created'

    # Operators for absolute dates (timestamp comparison)
    ABSOLUTE_OPERATORS = {
        '>=': 'gte',
        '>': 'gt',
        '<=': 'lte',
        '<': 'lt',
        '=': 'date',
        ':': 'gte',
    }

    # Operators for relative dates (age comparison - inverted)
    # "age <= 5min" means "timestamp >= now-5min"
    RELATIVE_OPERATORS = {
        '>=': 'lte',  # age >= X means timestamp <= now-X (older)
        '>': 'lt',
        '<=': 'gte',  # age <= X means timestamp >= now-X (newer)
        '<': 'gt',
        '=': 'date',
        ':': 'gte',   # Default: "created:7d" = within last 7 days
    }

    def _parse_date_value(self, value):
        """Try to parse as relative then absolute date. Returns (datetime, is_relative) or (None, False)."""
        date_value = parse_relative_date(value)
        if date_value is not None:
            return date_value, True
        try:
            return datetime.fromisoformat(value), False
        except ValueError:
            return None, False

    def validate(self, query: FieldQuery) -> list:
        date_value, _ = self._parse_date_value(query.value)
        if date_value is None:
            return [
                f'Invalid date value "{query.value}" in {query.field}{query.operator}{query.value}. '
                f'Use relative (e.g. 7d, 30min, 2w) or absolute (e.g. 2024-01-15) format.'
            ]
        return []

    def to_q(self, query: FieldQuery) -> Q:
        date_value, is_relative = self._parse_date_value(query.value)

        if date_value is None:
            return Q()  # Invalid date, return empty

        # Use appropriate operator mapping
        operators = self.RELATIVE_OPERATORS if is_relative else self.ABSOLUTE_OPERATORS
        lookup = operators.get(query.operator, 'gte')

        if lookup == 'date':
            # For exact date, match the whole day
            q = Q(telegram_date__date=date_value.date())
        else:
            q = Q(**{f'telegram_date__{lookup}': date_value})

        return ~q if query.negated else q


class HasMediaFilter(BaseFilter):
    """Filter by media presence."""

    field_name = 'has_media'

    def to_q(self, query: FieldQuery) -> Q:
        has_media = query.value.lower() in ('true', '1', 'yes')
        q = Q(has_media=has_media)
        return ~q if query.negated else q


class MediaTypeFilter(BaseFilter):
    """Filter by media type."""

    field_name = 'media_type'

    def to_q(self, query: FieldQuery) -> Q:
        q = Q(media_type__iexact=query.value)
        return ~q if query.negated else q


class DownloadedFilter(BaseFilter):
    """Filter by whether the message's media has been downloaded."""

    field_name = 'downloaded'

    def to_q(self, query: FieldQuery) -> Q:
        is_downloaded = query.value.lower() in ('true', '1', 'yes')
        q = Q(downloaded_file__isnull=not is_downloaded)
        return ~q if query.negated else q


class DeletedFilter(BaseFilter):
    """Filter by whether the message was deleted from its source."""

    field_name = 'deleted'

    def to_q(self, query: FieldQuery) -> Q:
        is_deleted = query.value.lower() in ('true', '1', 'yes')
        q = Q(is_deleted=is_deleted)
        return ~q if query.negated else q


class TagFilter(BaseFilter):
    """Filter messages by tag on their channel."""

    field_name = 'tag'

    def to_q(self, query: FieldQuery) -> Q:
        value = query.value
        if query.exact:
            q = Q(channel__tags__name__iexact=value)
        else:
            q = Q(channel__tags__name__icontains=value)
        return ~q if query.negated else q


class Sha256Filter(BaseFilter):
    """Filter by SHA256 hash of the downloaded file."""

    field_name = 'sha256'

    def to_q(self, query: FieldQuery) -> Q:
        q = Q(downloaded_file__sha256_hash__iexact=query.value.lower())
        return ~q if query.negated else q


class ArchivedFilter(BaseFilter):
    """
    Directive filter that controls whether archived sources are included.

    This is a view-level directive — it doesn't produce a message-level Q filter.
    The actual filtering is handled by extract_archived_mode() + view logic.
    Registered here so the query validator doesn't flag it as an unknown field.
    """

    field_name = 'archived'

    def to_q(self, query: FieldQuery) -> Q:
        return Q()  # No-op — handled at view level


def extract_archived_mode(query_string: str) -> str:
    """
    Extract the archived: directive from a query string.

    Returns:
        'active'   – default, only active sources (no archived: or archived:false)
        'archived' – only archived sources (archived:true)
        'all'      – both active and archived (archived:all)
    """
    if not query_string:
        return 'active'

    ast = parse_query(query_string)
    return _find_archived_value(ast)


def _find_archived_value(node: SearchNode) -> str:
    """Walk the AST to find an archived: field query."""
    if isinstance(node, FieldQuery) and node.field.lower() == 'archived':
        val = node.value.lower()
        if val in ('true', '1', 'yes'):
            return 'archived'
        if val in ('all', 'any', 'both'):
            return 'all'
        return 'active'

    if isinstance(node, (AndNode, OrNode)):
        for child in node.children:
            result = _find_archived_value(child)
            if result != 'active':
                return result

    return 'active'


# Registry of filters
FILTERS = {
    'text': TextFilter(),
    'url': UrlFilter(),
    'domain': DomainFilter(),
    'mention': MentionFilter(),
    'hashtag': HashtagFilter(),
    'email': EmailFilter(),
    'phone': PhoneFilter(),
    'channel': ChannelFilter(),
    'group': ChannelFilter(), # Alias of channel
    'source': ChannelFilter(), # Alias of channel
    'sender': SenderFilter(),
    'username': SenderFilter(), # Alias of sender
    'user': SenderFilter(), # Alias of sender
    'date': DateFilter(),
    'created': DateFilter(), # Alias of date
    'has_media': HasMediaFilter(),
    'media': HasMediaFilter(),  # Alias of has_media3
    'media_type': MediaTypeFilter(),
    'type': MediaTypeFilter(),  # Alias of media_type
    'downloaded': DownloadedFilter(),
    'deleted': DeletedFilter(),
    'tag': TagFilter(),
    'archived': ArchivedFilter(),
    'sha256': Sha256Filter(),
    'hash': Sha256Filter(),  # Alias of sha256
}


def ast_to_q(node: SearchNode) -> Q:
    """Convert AST to Django Q object."""

    if isinstance(node, FieldQuery):
        filter_cls = FILTERS.get(node.field.lower())
        if filter_cls:
            return filter_cls.to_q(node)
        # Unknown field - treat as text search
        return TextFallbackFilter().to_q(FieldQuery(
            field='text',
            value=f'{node.field}:{node.value}',
            negated=node.negated
        ))

    if isinstance(node, BareQuery):
        # Default to text search
        return TextFallbackFilter().to_q(FieldQuery(
            field='text',
            value=node.value,
            negated=node.negated
        ))

    if isinstance(node, AndNode):
        if not node.children:
            return Q()
        q = Q()
        for child in node.children:
            q &= ast_to_q(child)
        return ~q if node.negated else q

    if isinstance(node, OrNode):
        if not node.children:
            return Q()
        q = Q()
        for i, child in enumerate(node.children):
            if i == 0:
                q = ast_to_q(child)
            else:
                q |= ast_to_q(child)
        return ~q if node.negated else q

    return Q()


def validate_query(query_string: str) -> list:
    """
    Validate a search query and return a list of warning strings.
    Empty list = query is fully valid.
    """
    if not query_string or not query_string.strip():
        return []

    ast = parse_query(query_string)
    warnings = []
    _collect_warnings(ast, warnings)
    return warnings


def _collect_warnings(node: SearchNode, warnings: list):
    """Recursively walk AST and collect validation warnings."""
    if isinstance(node, FieldQuery):
        filter_cls = FILTERS.get(node.field.lower())
        if filter_cls:
            warnings.extend(filter_cls.validate(node))
        else:
            warnings.append(
                f'Unknown field "{node.field}" — treated as text search.'
            )
    elif isinstance(node, (AndNode, OrNode)):
        for child in node.children:
            _collect_warnings(child, warnings)


def search_messages(query_string: str, base_queryset=None):
    """
    Execute a search query and return filtered queryset.

    Args:
        query_string: The JQL/EQL like query string
        base_queryset: Optional base queryset to filter (defaults to all ArchivedMessages)

    Returns:
        Filtered QuerySet with search results
    """
    if base_queryset is None:
        base_queryset = ArchivedMessage.objects.from_active_accounts()

    if not query_string or not query_string.strip():
        return base_queryset

    ast = parse_query(query_string)
    q = ast_to_q(ast)

    return base_queryset.filter(q)
