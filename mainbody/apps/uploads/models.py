import uuid
from django.conf import settings
from django.db import models
from django.db.models import F, Q
from apps.common.models import BaseModel


class StorageQuota(BaseModel):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    activity = models.ForeignKey("activities.Activity", on_delete=models.PROTECT)
    limit_bytes = models.PositiveBigIntegerField()
    used_bytes = models.PositiveBigIntegerField(default=0)
    reserved_bytes = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "activity"], name="unique_owner_activity_quota"),
            models.CheckConstraint(condition=Q(limit_bytes__gte=F("used_bytes") + F("reserved_bytes")), name="quota_cannot_oversubscribe"),
        ]


class UploadSession(BaseModel):
    class Status(models.TextChoices):
        RESERVED = "reserved", "已预留额度"
        UPLOADING = "uploading", "上传中"
        COMPLETING = "completing", "合并中"
        COMPLETE = "complete", "已合并"
        ABORTING = "aborting", "取消中"
        ABORTED = "aborted", "已取消"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    requirement = models.ForeignKey("activities.MaterialRequirement", on_delete=models.PROTECT)
    quota = models.ForeignKey(StorageQuota, on_delete=models.PROTECT)
    request_key = models.UUIDField(default=uuid.uuid4)
    object_key = models.CharField(max_length=255, unique=True)
    original_name = models.CharField(max_length=255)
    declared_bytes = models.PositiveBigIntegerField()
    storage_upload_id = models.CharField(max_length=512, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RESERVED)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "request_key"], name="unique_owner_upload_request"),
            models.CheckConstraint(condition=Q(declared_bytes__gt=0) & Q(declared_bytes__lte=10_000_000_000), name="upload_ten_gb_ceiling"),
        ]
        indexes = [models.Index(fields=["owner", "status"]), models.Index(fields=["status", "expires_at"])]


class FileAsset(BaseModel):
    class Status(models.TextChoices):
        PENDING = "pending", "待检查"
        VALID = "valid", "检查通过"
        INVALID = "invalid", "检查失败"
        MANUAL = "manual", "需人工检查"

    upload = models.OneToOneField(UploadSession, on_delete=models.PROTECT)
    actual_bytes = models.PositiveBigIntegerField()
    detected_type = models.CharField(max_length=100, blank=True)
    checksum_algorithm = models.CharField(max_length=20, blank=True)
    checksum_value = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
