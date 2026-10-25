import pytest
from django.db import transaction
from django.utils import timezone
from apps.jobs.services import enqueue
from apps.jobs.models import OutboxEvent
from apps.jobs.tasks import consume_event
from apps.jobs.dispatcher import dispatch_one


@pytest.mark.django_db(transaction=True)
def test_outbox_shares_commit_and_rollback():
    with pytest.raises(RuntimeError):
        enqueue(topic="system.probe", dedup_key="outside", payload={})
    with pytest.raises(ValueError):
        with transaction.atomic():
            enqueue(topic="system.probe", dedup_key="rollback", payload={})
            raise ValueError("rollback")
    assert not OutboxEvent.objects.exists()


@pytest.mark.django_db
def test_duplicate_delivery_is_harmless():
    with transaction.atomic():
        event = enqueue(topic="system.probe", dedup_key="duplicate", payload={})
    consume_event.run(str(event.pk))
    event.refresh_from_db()
    processed_at = event.processed_at
    consume_event.run(str(event.pk))
    event.refresh_from_db()
    assert event.state == OutboxEvent.State.DONE
    assert event.processed_at == processed_at


@pytest.mark.django_db
def test_unimplemented_handler_fails_explicitly():
    with transaction.atomic():
        event = enqueue(topic="files.validate", dedup_key="no-handler", payload={"file_id": "placeholder"})
    consume_event.run(str(event.pk))
    event.refresh_from_db()
    assert event.state == OutboxEvent.State.FAILED
    assert event.last_error_code == "handler_not_implemented"


@pytest.mark.django_db
def test_broker_outage_keeps_event_for_retry(monkeypatch):
    with transaction.atomic():
        event = enqueue(topic="system.probe", dedup_key="retry", payload={})
    def unavailable(*args, **kwargs):
        raise ConnectionError("Do not log credentials in exception text")
    monkeypatch.setattr("apps.jobs.dispatcher.app.send_task", unavailable)
    assert dispatch_one()
    event.refresh_from_db()
    assert event.state == OutboxEvent.State.PENDING
    assert event.attempts == 1
    assert event.available_at > timezone.now()
    assert event.lease_token is None
    event.available_at = timezone.now()
    event.save(update_fields=["available_at"])
    monkeypatch.setattr("apps.jobs.dispatcher.app.send_task", lambda *a, **kw: None)
    assert dispatch_one()
    event.refresh_from_db()
    assert event.attempts == 2
    assert event.state == OutboxEvent.State.PENDING  # Publish is not proof of consumption.


@pytest.mark.django_db
def test_retry_budget_is_finite(settings, monkeypatch):
    settings.OUTBOX_MAX_ATTEMPTS = 1
    event = OutboxEvent.objects.create(topic="system.probe", dedup_key="exhausted", attempts=1)
    def must_not_publish(*args, **kwargs):
        pytest.fail("exhausted event should not be published")
    monkeypatch.setattr("apps.jobs.dispatcher.app.send_task", must_not_publish)
    assert dispatch_one()
    event.refresh_from_db()
    assert event.state == OutboxEvent.State.FAILED
