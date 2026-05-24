"""
RabbitMQ publish delivery target.
Publishes the payload as a persistent JSON message to a user-named queue.
Mirrors the connection pattern used by events.processor._stream_raw_event.
"""

import json
import logging

import pika
from django.conf import settings

logger = logging.getLogger('trawlr.notifications.rabbitmq')


class RabbitMQDeliveryError(RuntimeError):
    pass


def _normalize_url(url: str) -> str:
    return url[:-1] if url.endswith('//') else url


def deliver_rabbitmq(delivery) -> None:
    cfg = delivery.entry.target_config or {}
    queue = (cfg.get('queue') or '').strip()
    if not queue:
        raise RabbitMQDeliveryError("target_config.queue is required")
    exchange = (cfg.get('exchange') or '').strip()
    routing_key = (cfg.get('routing_key') or '').strip() or queue
    declare = bool(cfg.get('declare', False))

    rabbitmq_url = _normalize_url(settings.RABBITMQ_URL)
    body = json.dumps(delivery.event_payload, separators=(',', ':')).encode('utf-8')

    try:
        params = pika.URLParameters(rabbitmq_url)
        connection = pika.BlockingConnection(params)
    except Exception as e:
        raise RabbitMQDeliveryError(f"Could not connect to RabbitMQ: {e}") from e

    try:
        channel = connection.channel()
        if declare:
            channel.queue_declare(queue=queue, durable=True)
        else:
            channel.queue_declare(queue=queue, passive=True)

        channel.basic_publish(
            exchange=exchange,
            routing_key=routing_key,
            body=body,
            properties=pika.BasicProperties(
                delivery_mode=2,
                content_type='application/json',
                headers={
                    'X-Trawlr-Delivery-Id': str(delivery.pk),
                    'X-Trawlr-Entry-Id': str(delivery.entry_id),
                },
            ),
        )
        logger.info(
            "Published delivery=%s entry=%s to exchange=%r routing_key=%r",
            delivery.pk, delivery.entry_id, exchange, routing_key,
        )
    except Exception as e:
        raise RabbitMQDeliveryError(f"Publish failed: {e}") from e
    finally:
        try:
            connection.close()
        except Exception:
            pass
