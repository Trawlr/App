"""
Configuration for the listener service.

Loads settings from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass


@dataclass
class Config:
    """Listener service configuration."""

    # RabbitMQ
    rabbitmq_url: str

    # Health check server
    health_port: int = 8001

    # Polling intervals
    account_poll_interval: int = 30  # seconds
    health_check_interval: int = 60  # seconds

    # Reconnection settings
    initial_backoff: int = 5  # seconds
    max_backoff: int = 300  # 5 minutes

    # RabbitMQ command queue
    command_exchange: str = 'trawlr.listener.commands'
    command_queue: str = 'trawlr.listener.commands'

    @classmethod
    def from_environment(cls) -> 'Config':
        """Load configuration from environment variables."""
        rabbitmq_url = os.environ.get('RABBITMQ_URL')
        if not rabbitmq_url:
            raise ValueError("RABBITMQ_URL environment variable is required")

        # Normalize vhost in URL - aio_pika needs proper vhost encoding
        # URLs ending with // should be converted to /%2F for the default vhost /
        if rabbitmq_url.endswith('//'):
            rabbitmq_url = rabbitmq_url[:-2] + '/%2F'

        return cls(
            rabbitmq_url=rabbitmq_url,
            health_port=int(os.environ.get('LISTENER_HEALTH_PORT', '8001')),
            account_poll_interval=int(os.environ.get('LISTENER_POLL_INTERVAL', '30')),
            health_check_interval=int(os.environ.get('LISTENER_HEALTH_INTERVAL', '60')),
            initial_backoff=int(os.environ.get('LISTENER_INITIAL_BACKOFF', '5')),
            max_backoff=int(os.environ.get('LISTENER_MAX_BACKOFF', '300')),
        )


# Global config instance
config: Config | None = None


def get_config() -> Config:
    """Get the global configuration instance."""
    global config
    if config is None:
        config = Config.from_environment()
    return config
