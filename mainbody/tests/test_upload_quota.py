import uuid
import pytest
from apps.common.errors import Conflict, DomainError, QuotaExceeded
from apps.uploads.models import StorageQuota, UploadSession
from apps.uploads.services import reserve_upload, cancel_unstarted_reservation


def reserve(student, rule, size, key=None):
    return reserve_upload(actor=student, requirement_id=rule.pk, filename="材料.zip",
                          declared_bytes=size, request_key=key or uuid.uuid4())


def test_duplicate_reservation_does_not_double_charge(student, rule):
    key = uuid.uuid4()
    first = reserve(student, rule, 100, key)
    assert reserve(student, rule, 100, key).pk == first.pk
    assert StorageQuota.objects.get(owner=student).reserved_bytes == 100
    with pytest.raises(Conflict):
        reserve(student, rule, 101, key)


def test_aggregate_quota_rolls_back_failed_reservation(student, rule):
    reserve(student, rule, 6_000_000_000)
    with pytest.raises(QuotaExceeded):
        reserve(student, rule, 6_000_000_000)
    assert UploadSession.objects.count() == 1
    assert StorageQuota.objects.get(owner=student).reserved_bytes == 6_000_000_000


def test_ten_gb_is_a_hard_ceiling(student, rule):
    with pytest.raises(DomainError):
        reserve(student, rule, 10_000_000_001)
    assert not StorageQuota.objects.exists()


def test_cancel_is_idempotent_and_remote_upload_is_not_released(student, rule):
    upload = reserve(student, rule, 100)
    cancel_unstarted_reservation(actor=student, upload_id=upload.pk)
    cancel_unstarted_reservation(actor=student, upload_id=upload.pk)
    assert StorageQuota.objects.get(owner=student).reserved_bytes == 0
    other = reserve(student, rule, 200)
    other.storage_upload_id = "remote-task"
    other.save(update_fields=["storage_upload_id"])
    with pytest.raises(Conflict):
        cancel_unstarted_reservation(actor=student, upload_id=other.pk)
    assert StorageQuota.objects.get(owner=student).reserved_bytes == 200


def test_upload_parallelism_is_bounded(student, rule):
    reserve(student, rule, 1)
    reserve(student, rule, 1)
    with pytest.raises(Conflict):
        reserve(student, rule, 1)


def test_missing_extension_policy_denies_upload(student, rule):
    rule.allowed_extensions = []
    rule.save(update_fields=["allowed_extensions"])
    with pytest.raises(DomainError):
        reserve(student, rule, 1)
