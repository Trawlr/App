"""
Custom entity parser system.

This module provides a pluggable parser pipeline that runs during event processing
to extract custom entities beyond what Telethon provides.

Usage:
    from parsers import run_parsers

    # In event processor:
    additional_entities = run_parsers(payload, existing_entities)
    all_entities = existing_entities + additional_entities
"""
import logging
from typing import TYPE_CHECKING

from .base import BaseParser
from .domain_parser import DomainParser

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)

# Registry of enabled parsers (instantiated)
_PARSERS: list[BaseParser] = [
    DomainParser(),
]


def get_registered_parsers() -> list[BaseParser]:
    """Return list of all registered parsers."""
    return _PARSERS.copy()


def register_parser(parser: BaseParser) -> None:
    """Register a new parser at runtime."""
    if not isinstance(parser, BaseParser):
        raise TypeError(f"Parser must be a BaseParser instance, got {type(parser)}")
    _PARSERS.append(parser)


def run_parsers(payload: 'dict[str, Any]', entities: 'list[dict]') -> 'list[dict]':
    """
    Run all registered parsers and return additional entities.

    Args:
        payload: Full message event payload
        entities: Existing entities from Telethon extraction

    Returns:
        List of all additional entities from all parsers
    """
    additional_entities = []

    for parser in _PARSERS:
        try:
            new_entities = parser.parse(payload, entities + additional_entities)
            if new_entities:
                additional_entities.extend(new_entities)
                logger.debug(
                    f"Parser '{parser.name}' added {len(new_entities)} entities "
                    f"for message {payload.get('message_id')}"
                )
        except Exception as e:
            logger.exception(f"Parser '{parser.name}' failed: {e}")

    return additional_entities
