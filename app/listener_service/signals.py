"""
Django integration for the listener service.

Provides functions to send commands to the dedicated listener service
via RabbitMQ. Used by Django views and admin actions.
"""

import json
import logging

import pika
from django.conf import settings

logger = logging.getLogger('trawlr.listener_service.signals')


def get_rabbitmq_connection():
    """Create a blocking RabbitMQ connection."""
    # Pika needs the vhost properly encoded. URLs ending with // need to be
    # converted to /%2F for the default vhost /
    rabbitmq_url = settings.RABBITMQ_URL
    if rabbitmq_url.endswith('//'):
        rabbitmq_url = rabbitmq_url[:-2] + '/%2F'
    params = pika.URLParameters(rabbitmq_url)
    return pika.BlockingConnection(params)


def send_listener_command(command: str, account_id: int | None = None):
    """
    Send a command to the listener service via RabbitMQ.

    Commands:
    - start: Start listener for account_id
    - stop: Stop listener for account_id
    - restart: Restart listener for account_id
    - restart_all: Restart all listeners (no account_id needed)
    - status: Log current status (no account_id needed)
    """
    exchange_name = 'trawlr.listener.commands'

    message = {
        'command': command,
    }
    if account_id is not None:
        message['account_id'] = account_id

    try:
        connection = get_rabbitmq_connection()
        channel = connection.channel()

        # Declare exchange (idempotent)
        channel.exchange_declare(
            exchange=exchange_name,
            exchange_type='fanout',
            durable=True
        )

        # Publish message
        channel.basic_publish(
            exchange=exchange_name,
            routing_key='',
            body=json.dumps(message).encode(),
            properties=pika.BasicProperties(
                content_type='application/json',
                delivery_mode=2,  # Persistent
            )
        )

        connection.close()
        logger.info(f"Sent listener command: {command} for account {account_id}")
        return True

    except Exception as e:
        logger.exception(f"Failed to send listener command: {e}")
        return False


def start_listener(account_id: int) -> bool:
    """Start the listener for an account."""
    return send_listener_command('start', account_id)


def stop_listener(account_id: int) -> bool:
    """Stop the listener for an account."""
    return send_listener_command('stop', account_id)


def restart_listener(account_id: int) -> bool:
    """Restart the listener for an account."""
    return send_listener_command('restart', account_id)


def restart_all_listeners() -> bool:
    """Restart all listeners."""
    return send_listener_command('restart_all')


def start_listener_for_account(account):
    """
    Start the listener for an account using the dedicated listener service.

    Args:
        account: TelegramAccount instance

    Returns:
        bool: True if command was sent successfully
    """
    return start_listener(account.pk)


def stop_listener_for_account(account):
    """
    Stop the listener for an account using the dedicated listener service.

    Args:
        account: TelegramAccount instance

    Returns:
        bool: True if command was sent successfully
    """
    return stop_listener(account.pk)
