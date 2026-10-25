from django.db import connection
from apps.common.errors import Conflict
from .models import OutboxEvent
from .registry import QUEUES


def enqueue(*, topic, dedup_key, payload):
    """Call INSIDE the transaction that changes the associated business state."""
    if not connection.in_atomic_block:
        raise RuntimeError("enqueue() must share the business transaction")
    if topic not in QUEUES:
        raise ValueError("Unknown event topic")
    if not isinstance(payload, dict):
        raise ValueError("Payload must be a JSON object of resource identifiers")
    event, _ = OutboxEvent.objects.get_or_create(
        dedup_key=dedup_key, defaults={"topic": topic, "payload": payload})
    if event.topic != topic or event.payload != payload:
        raise Conflict("任务幂等键已用于不同的事件。")
    return event
