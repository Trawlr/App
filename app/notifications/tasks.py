"""
Dramatiq actor that performs external delivery of a NotificationDelivery row.

The matcher creates the NotificationDelivery row synchronously (in the event
processor) and dispatches this actor. On failure we re-enqueue the actor with
an explicit delay drawn from BACKOFF_SCHEDULE_MS (15s, 30s, 60s, 5m, 1h).
After the schedule is exhausted the row flips to status='exhausted' and we
stop trying. ActivityLog rows are written by the matcher and are not touched
here.

Dramatiq's own retry middleware is disabled on this actor (max_retries=0) so
the schedule below is the single source of truth for retry timing.
"""

import trawlr.dramatiq_config  # noqa: F401
import logging
import dramatiq
from django.utils import timezone
from .delivery.rabbitmq import deliver_rabbitmq
from .delivery.webhook import deliver_webhook
from .models import (
    NotificationDelivery,
    STATUS_DELIVERED,
    STATUS_EXHAUSTED,
    STATUS_FAILED,
    TARGET_RABBITMQ,
    TARGET_WEBHOOK,
)

logger = logging.getLogger('trawlr.notifications.tasks')

QUEUE_NOTIFICATIONS = 'trawlr.notifications'

# Delay (ms) before each retry. 15s, 30s, 1m, 5m, 1h before marking notification as failed.
BACKOFF_SCHEDULE_MS = [15_000, 30_000, 60_000, 300_000, 3_600_000]
MAX_ATTEMPTS = len(BACKOFF_SCHEDULE_MS) + 1


@dramatiq.actor(
    queue_name=QUEUE_NOTIFICATIONS,
    max_retries=0,  # custom retry schedule, see BACKOFF_SCHEDULE_MS
)
def deliver_notification(delivery_id: int):
    try:
        delivery = NotificationDelivery.objects.select_related('entry').get(pk=delivery_id)
    except NotificationDelivery.DoesNotExist:
        logger.warning("NotificationDelivery %s not found, skipping", delivery_id)
        return

    if delivery.status == STATUS_DELIVERED:
        return

    delivery.attempts += 1
    delivery.last_attempt_at = timezone.now()

    try:
        if delivery.entry.target_type == TARGET_WEBHOOK:
            deliver_webhook(delivery)
        elif delivery.entry.target_type == TARGET_RABBITMQ:
            deliver_rabbitmq(delivery)
        else:
            raise RuntimeError(f"Unknown target_type: {delivery.entry.target_type!r}")

        delivery.status = STATUS_DELIVERED
        delivery.delivered_at = timezone.now()
        delivery.last_error = ''
        delivery.save(update_fields=['status', 'delivered_at', 'last_error',
                                     'attempts', 'last_attempt_at'])
        logger.info("Delivered notification %s (entry=%s)", delivery.pk, delivery.entry_id)
        return

    except Exception as exc:
        delivery.last_error = f"{type(exc).__name__}: {exc}"[:4000]

        if delivery.attempts >= MAX_ATTEMPTS:
            delivery.status = STATUS_EXHAUSTED
            delivery.save(update_fields=['status', 'last_error',
                                         'attempts', 'last_attempt_at'])
            logger.error(
                "Delivery %s (entry=%s) exhausted after %s attempts: %s",
                delivery.pk, delivery.entry_id, delivery.attempts, delivery.last_error,
            )
            return

        delivery.status = STATUS_FAILED
        delivery.save(update_fields=['status', 'last_error',
                                     'attempts', 'last_attempt_at'])

        delay_ms = BACKOFF_SCHEDULE_MS[delivery.attempts - 1]
        deliver_notification.send_with_options(args=(delivery_id,), delay=delay_ms)
        logger.warning(
            "Delivery %s (entry=%s) failed on attempt %s, retrying in %ds: %s",
            delivery.pk, delivery.entry_id, delivery.attempts,
            delay_ms // 1000, delivery.last_error,
        )
