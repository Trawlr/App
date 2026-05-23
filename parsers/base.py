"""
Base parser class for custom entity extraction.

Parsers run in the event processor (worker) after Telethon entity extraction,
allowing custom entity types to be added before database storage.
"""
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any


class BaseParser(ABC):
    """
    Abstract base class for custom entity parsers.

    Parsers receive the full message payload and existing entities,
    and can add new entities to be stored in the database.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this parser."""
        pass

    @abstractmethod
    def parse(self, payload: 'dict[str, Any]', entities: 'list[dict]') -> 'list[dict]':
        """
        Parse the message and return additional entities.

        Args:
            payload: Full message event payload containing:
                - message_data: dict with 'text', 'date', etc.
                - sender_data: dict with sender info
                - media_info: dict with media metadata
                - entities_data: list of existing entities
                - chat_id, message_id, etc.
            entities: Current list of entities (may include previously added custom entities)

        Returns:
            List of new entity dicts to add. Each entity should have:
                - entity_type: str (e.g., 'domain', 'custom_type')
                - offset: int (position in text, or 0 for derived entities)
                - length: int (length in text, or 0 for derived entities)
                - text: str (the extracted value)
                - url: str (optional, related URL)
                - user_id: int | None (optional)
                - language: str (optional)
                - custom_emoji_id: int | None (optional)
        """
        pass
