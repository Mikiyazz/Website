from celery import shared_task
from django.db import transaction
from django.utils import timezone
from .models import OutboxEvent
from .registry import handle_event


@shared_task(name="apps.jobs.consume_event", ignore_result=True)
def consume_event(event_id):
    with transaction.atomic():
        event = OutboxEvent.objects.select_for_update().get(pk=event_id)
        if event.state != OutboxEvent.State.PENDING:
            return  # Duplicate delivery is an expected case.
        try:
            handle_event(event)
        except NotImplementedError:
            event.state = OutboxEvent.State.FAILED
            event.last_error_code = "handler_not_implemented"
        else:
            event.state = OutboxEvent.State.DONE
            event.last_error_code = ""
            event.processed_at = timezone.now()
        event.lease_until = None
        event.lease_token = None
        event.save(update_fields=["state", "last_error_code", "processed_at", "lease_until", "lease_token", "updated_at"])
