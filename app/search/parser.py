"""
JQL-like query parser for Trawlr search.

Supported syntax:
  text:keyword          - Full-text search in message text (partial match)
  text:"exact phrase"   - Exact match (case-insensitive)
  url:example.com       - URL entity search (substring match)
  mention:@username     - Mention search
  hashtag:#tag          - Hashtag search
  email:*@domain.com    - Email search (wildcard support)
  phone:+1234           - Phone number search
  channel:channelname   - Filter by channel title/username
  sender:username       - Filter by sender username/name (partial match)
  sender:"exactuser"    - Exact match on sender
  created>=7d           - Relative date filter
  created>=2024-01-01   - Absolute date filter
  has_media:true/false  - Media presence filter
  media_type:photo      - Media type filter
  deleted:true/false    - Channel/chat deleted filter

Operators:
  AND (implicit between terms)
  OR  (explicit, lower precedence)
  NOT or - (prefix negation)

Grouping:
  Parentheses for grouping: (text:foo OR text:bar) AND channel:news

Quoting:
  Double quotes for exact match: sender:"r00tof" (matches only "r00tof", not "r00t")
  Without quotes: sender:r00tof (matches "r00t", "r00to", "r00tof", etc.)
"""

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional
from datetime import datetime, timedelta
from django.utils import timezone


class TokenType(Enum):
    FIELD_QUERY = auto()    # field:value
    BARE_WORD = auto()      # unqualified search term
    AND = auto()
    OR = auto()
    NOT = auto()
    LPAREN = auto()
    RPAREN = auto()
    EOF = auto()


@dataclass
class Token:
    type: TokenType
    value: str
    field: Optional[str] = None
    operator: str = ':'  # :, >=, <=, >, <, =
    exact: bool = False  # True if value was quoted (exact match)


@dataclass
class SearchNode:
    """Base class for AST nodes."""
    pass


@dataclass
class FieldQuery(SearchNode):
    """A field:value query."""
    field: str
    value: str
    operator: str = ':'
    negated: bool = False
    exact: bool = False  # True if value was quoted (exact match)


@dataclass
class BareQuery(SearchNode):
    """Unqualified search term (searches text by default)."""
    value: str
    negated: bool = False


@dataclass
class AndNode(SearchNode):
    """AND conjunction."""
    children: List[SearchNode] = field(default_factory=list)
    negated: bool = False


@dataclass
class OrNode(SearchNode):
    """OR disjunction."""
    children: List[SearchNode] = field(default_factory=list)
    negated: bool = False


class SearchLexer:
    """Tokenize a search query string."""

    FIELD_PATTERN = re.compile(
        r'(-)?(\w+)(>=|<=|>|<|=|!:|:)("(?:[^"\\]|\\.)*"|[^\s()]+)'
    )
    QUOTED_PATTERN = re.compile(r'"(?:[^"\\]|\\.)*"')

    def __init__(self, query: str):
        self.query = query
        self.pos = 0
        self.tokens: List[Token] = []

    def tokenize(self) -> List[Token]:
        while self.pos < len(self.query):
            self._skip_whitespace()
            if self.pos >= len(self.query):
                break

            char = self.query[self.pos]

            if char == '(':
                self.tokens.append(Token(TokenType.LPAREN, '('))
                self.pos += 1
            elif char == ')':
                self.tokens.append(Token(TokenType.RPAREN, ')'))
                self.pos += 1
            elif self._match_keyword('AND'):
                self.tokens.append(Token(TokenType.AND, 'AND'))
            elif self._match_keyword('OR'):
                self.tokens.append(Token(TokenType.OR, 'OR'))
            elif self._match_keyword('NOT'):
                self.tokens.append(Token(TokenType.NOT, 'NOT'))
            elif char == '-' and self._peek_is_term():
                self.tokens.append(Token(TokenType.NOT, 'NOT'))
                self.pos += 1
            else:
                self._parse_term()

        self.tokens.append(Token(TokenType.EOF, ''))
        return self.tokens

    def _skip_whitespace(self):
        while self.pos < len(self.query) and self.query[self.pos].isspace():
            self.pos += 1

    def _peek_is_term(self) -> bool:
        """Check if next char after - is start of a term or group."""
        next_pos = self.pos + 1
        if next_pos >= len(self.query):
            return False
        next_char = self.query[next_pos]
        return next_char.isalnum() or next_char in '"('

    def _match_keyword(self, keyword: str) -> bool:
        end = self.pos + len(keyword)
        if (self.query[self.pos:end].upper() == keyword and
            (end >= len(self.query) or not self.query[end].isalnum())):
            self.pos = end
            return True
        return False

    def _parse_term(self):
        match = self.FIELD_PATTERN.match(self.query, self.pos)
        if match:
            negated = match.group(1) == '-'
            field = match.group(2)
            operator = match.group(3)
            raw_value = match.group(4)
            # Check if value was quoted (exact match requested)
            exact = raw_value.startswith('"') and raw_value.endswith('"')
            value = raw_value.strip('"')

            if negated:
                self.tokens.append(Token(TokenType.NOT, 'NOT'))

            self.tokens.append(Token(
                TokenType.FIELD_QUERY,
                value,
                field=field,
                operator=operator,
                exact=exact
            ))
            self.pos = match.end()
        else:
            # Bare word or quoted phrase
            if self.query[self.pos] == '"':
                match = self.QUOTED_PATTERN.match(self.query, self.pos)
                if match:
                    value = match.group()[1:-1]  # Strip quotes
                    self.tokens.append(Token(TokenType.BARE_WORD, value))
                    self.pos = match.end()
                    return

            # Simple word
            start = self.pos
            while (self.pos < len(self.query) and
                   not self.query[self.pos].isspace() and
                   self.query[self.pos] not in '()'):
                self.pos += 1

            if self.pos > start:
                self.tokens.append(Token(
                    TokenType.BARE_WORD,
                    self.query[start:self.pos]
                ))


class SearchParser:
    """Parse tokens into an AST."""

    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def parse(self) -> SearchNode:
        result = self._parse_or()
        return result

    def _current(self) -> Token:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return Token(TokenType.EOF, '')

    def _advance(self):
        self.pos += 1

    def _parse_or(self) -> SearchNode:
        left = self._parse_and()

        while self._current().type == TokenType.OR:
            self._advance()
            right = self._parse_and()
            if isinstance(left, OrNode):
                left.children.append(right)
            else:
                left = OrNode(children=[left, right])

        return left

    def _parse_and(self) -> SearchNode:
        left = self._parse_unary()

        while self._current().type in (TokenType.AND, TokenType.FIELD_QUERY,
                                       TokenType.BARE_WORD, TokenType.NOT,
                                       TokenType.LPAREN):
            if self._current().type == TokenType.AND:
                self._advance()
            right = self._parse_unary()
            if isinstance(left, AndNode):
                left.children.append(right)
            else:
                left = AndNode(children=[left, right])

        return left

    def _parse_unary(self) -> SearchNode:
        negated = False
        if self._current().type == TokenType.NOT:
            negated = True
            self._advance()

        node = self._parse_primary()

        if negated:
            # Apply negation to any node type
            node.negated = True

        return node

    def _parse_primary(self) -> SearchNode:
        token = self._current()

        if token.type == TokenType.LPAREN:
            self._advance()
            node = self._parse_or()
            if self._current().type == TokenType.RPAREN:
                self._advance()
            return node

        if token.type == TokenType.FIELD_QUERY:
            self._advance()
            return FieldQuery(
                field=token.field,
                value=token.value,
                operator=token.operator,
                exact=token.exact
            )

        if token.type == TokenType.BARE_WORD:
            self._advance()
            return BareQuery(value=token.value)

        # Default empty
        return AndNode(children=[])


def parse_query(query_string: str) -> SearchNode:
    """Parse a query string into an AST."""
    if not query_string or not query_string.strip():
        return AndNode(children=[])

    lexer = SearchLexer(query_string)
    tokens = lexer.tokenize()
    parser = SearchParser(tokens)
    return parser.parse()


def parse_relative_date(value: str) -> Optional[datetime]:
    """
    Parse relative dates like 30s, 5min, 2h, 7d, 2w, 1mo, 1y.

    Supported units:
        s   - seconds
        min - minutes
        h   - hours
        d   - days
        w   - weeks
        mo  - months (30 days)
        m   - months (30 days) - legacy
        y   - years (365 days)
    """
    match = re.match(r'(\d+)(s|min|h|d|w|mo|m|y)$', value.lower())
    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)

    now = timezone.now()
    if unit == 's':
        return now - timedelta(seconds=amount)
    elif unit == 'min':
        return now - timedelta(minutes=amount)
    elif unit == 'h':
        return now - timedelta(hours=amount)
    elif unit == 'd':
        return now - timedelta(days=amount)
    elif unit == 'w':
        return now - timedelta(weeks=amount)
    elif unit in ('mo', 'm'):
        return now - timedelta(days=amount * 30)
    elif unit == 'y':
        return now - timedelta(days=amount * 365)

    return None
