"""Claim in DB, publish outside transaction, update by lease token.

Accepted messages are redelivered after a timeout until the consumer commits DONE.
Delivery is at least once, bounded to OUTBOX_MAX_ATTEMPTS. No exactly-once promise.
"""
import logging
import uuid
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from config.celery import app
from .models import OutboxEvent
from .registry import QUEUES

logger = logging.getLogger(__name__)


@transaction.atomic
def claim():
    now = timezone.now()
    event = (OutboxEvent.objects.select_for_update(skip_locked=True)
             .filter(state=OutboxEvent.State.PENDING, available_at__lte=now)
             .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
             .order_by("available_at", "created_at").first())
    if event is None:
        return None
    if event.attempts >= settings.OUTBOX_MAX_ATTEMPTS:
        event.state = OutboxEvent.State.FAILED
        event.last_error_code = "delivery_attempts_exhausted"
        event.save(update_fields=["state", "last_error_code", "updated_at"])
        logger.error("outbox_exhausted", extra={"event_id": str(event.pk)})
        return event
    event.attempts += 1
    event.lease_token = uuid.uuid4()
    event.lease_until = now + timedelta(seconds=settings.OUTBOX_LEASE_SECONDS)
    event.save(update_fields=["attempts", "lease_token", "lease_until", "updated_at"])
    return event


def dispatch_one():
    event = claim()
    if event is None:
        return False
    if event.state == OutboxEvent.State.FAILED:
        return True
    error_code = ""
    delay = settings.OUTBOX_REDELIVERY_SECONDS
    try:
        app.send_task("apps.jobs.consume_event", args=[str(event.pk)], queue=QUEUES[event.topic], retry=False)
    except Exception as exc:
        error_code = type(exc).__name__[:100]
        delay = min(300, 2 ** event.attempts)
        logger.warning("outbox_publish_failed", extra={"event_id": str(event.pk), "error_type": error_code})
    # Consumer may already have committed DONE; do not overwrite its result.
    OutboxEvent.objects.filter(pk=event.pk, lease_token=event.lease_token, state=OutboxEvent.State.PENDING).update(
        lease_until=None, lease_token=None, last_error_code=error_code,
        available_at=timezone.now() + timedelta(seconds=delay))
    return True
