"""Internal quota example, not yet exposed as a cloud upload endpoint.

Lock order: user -> quota -> upload. Use the same order in future finalize/abort.
Never release cloud-backed reservations until remote abort/completion is confirmed.
"""
import uuid
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from apps.accounts.models import User
from apps.accounts.permissions import require_student
from apps.activities.models import MaterialRequirement
from apps.activities.selectors import ensure_activity_open
from apps.common.errors import Conflict, DomainError, QuotaExceeded
from .models import StorageQuota, UploadSession
from .policies import validate_metadata

ACTIVE_STATES = [UploadSession.Status.RESERVED, UploadSession.Status.UPLOADING,
                 UploadSession.Status.COMPLETING, UploadSession.Status.ABORTING]


@transaction.atomic
def reserve_upload(*, actor, requirement_id, filename, declared_bytes, request_key):
    require_student(actor)
    try:
        request_key = uuid.UUID(str(request_key))
    except ValueError:
        raise DomainError("request_key 必须为 UUID。") from None
    # One user's competing reservations serialize; different users do not share this lock.
    owner = User.objects.select_for_update().get(pk=actor.pk)
    require_student(owner)
    existing = UploadSession.objects.filter(owner=owner, request_key=request_key).first()
    if existing:
        if (str(existing.requirement_id), existing.original_name, existing.declared_bytes) != (str(requirement_id), filename, declared_bytes):
            raise Conflict("同一幂等键不能用于不同的文件。")
        return existing
    rule = MaterialRequirement.objects.select_related("activity").get(pk=requirement_id)
    ensure_activity_open(rule.activity, owner)
    validate_metadata(filename=filename, declared_bytes=declared_bytes, requirement=rule)
    if UploadSession.objects.filter(owner=owner, status__in=ACTIVE_STATES).count() >= settings.MAX_ACTIVE_UPLOADS_PER_USER:
        raise Conflict("同时上传任务过多，请先完成或取消已有任务。")
    if UploadSession.objects.filter(owner=owner, requirement=rule).exclude(status=UploadSession.Status.ABORTED).count() >= rule.max_count:
        raise Conflict("此材料类别已达到文件数量限制。")
    quota, _ = StorageQuota.objects.get_or_create(owner=owner, activity=rule.activity,
                                                 defaults={"limit_bytes": rule.activity.quota_bytes_per_student})
    quota = StorageQuota.objects.select_for_update().get(pk=quota.pk)
    if quota.used_bytes + quota.reserved_bytes + declared_bytes > quota.limit_bytes:
        raise QuotaExceeded("该活动的累计存储额度不足。")
    quota.reserved_bytes += declared_bytes
    quota.save(update_fields=["reserved_bytes", "updated_at"])
    return UploadSession.objects.create(
        owner=owner, requirement=rule, quota=quota, request_key=request_key,
        original_name=filename, declared_bytes=declared_bytes,
        object_key=f"activities/{rule.activity_id}/{uuid.uuid4().hex}",
        expires_at=timezone.now() + timedelta(days=3),
    )


@transaction.atomic
def cancel_unstarted_reservation(*, actor, upload_id):
    """Only a purely local reservation can be released without a cloud API call."""
    require_student(actor)
    User.objects.select_for_update().get(pk=actor.pk)
    upload = UploadSession.objects.get(pk=upload_id, owner=actor)
    quota = StorageQuota.objects.select_for_update().get(pk=upload.quota_id)
    upload = UploadSession.objects.select_for_update().get(pk=upload.pk)
    if upload.status == UploadSession.Status.ABORTED:
        return upload
    if upload.status != UploadSession.Status.RESERVED or upload.storage_upload_id:
        raise Conflict("任务已涉及远程存储，必须先确认云端取消。")
    quota.reserved_bytes -= upload.declared_bytes
    quota.save(update_fields=["reserved_bytes", "updated_at"])
    upload.status = UploadSession.Status.ABORTED
    upload.save(update_fields=["status", "updated_at"])
    return upload
