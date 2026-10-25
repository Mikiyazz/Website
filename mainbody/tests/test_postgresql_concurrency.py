"""Real transaction tests; explicitly skipped by the SQLite smoke-test mode."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from django.db import connection, connections, close_old_connections
from apps.accounts.models import User
from apps.common.errors import Conflict, QuotaExceeded
from apps.uploads.models import StorageQuota, UploadSession
from apps.uploads.services import reserve_upload
from apps.submissions.services import create_draft, save_draft
from apps.activities.selectors import eligible_activities

pytestmark = [pytest.mark.postgresql, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def real_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Real PostgreSQL is required; SQLite cannot verify row locking.")


def test_parallel_reservations_cannot_overspend(student, rule):
    barrier = Barrier(2)
    def reserve():
        close_old_connections()
        try:
            actor = User.objects.get(pk=student.pk)
            barrier.wait(timeout=10)
            try:
                reserve_upload(actor=actor, requirement_id=rule.pk, filename="demo.zip",
                               declared_bytes=6_000_000_000, request_key=uuid.uuid4())
                return "reserved"
            except QuotaExceeded:
                return "denied"
        finally:
            connections.close_all()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert sorted(results) == ["denied", "reserved"]
    assert UploadSession.objects.count() == 1
    assert StorageQuota.objects.get(owner=student).reserved_bytes == 6_000_000_000


def test_parallel_draft_saves_detect_conflict(student, activity):
    draft = create_draft(actor=student, activity_id=activity.pk)
    barrier = Barrier(2)
    def save(index):
        close_old_connections()
        try:
            actor = User.objects.get(pk=student.pk)
            barrier.wait(timeout=10)
            try:
                save_draft(actor=actor, submission_id=draft.pk, expected_version=0, data={"writer": index})
                return "saved"
            except Conflict:
                return "conflict"
        finally:
            connections.close_all()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, range(2)))
    assert sorted(results) == ["conflict", "saved"]


def test_activity_audience_uses_postgresql_json(student, other_student, activity):
    activity.allowed_classes = [student.class_name]
    activity.save(update_fields=["allowed_classes"])
    assert eligible_activities(student).filter(pk=activity.pk).exists()
    assert not eligible_activities(other_student).filter(pk=activity.pk).exists()
