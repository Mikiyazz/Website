from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from apps.common.models import BaseModel


class Activity(BaseModel):
    title = models.CharField(max_length=200)
    opens_at = models.DateTimeField()
    deadline = models.DateTimeField()
    is_open = models.BooleanField(default=False)
    # Empty list means all active students. Explicit class list restricts access.
    allowed_classes = models.JSONField(default=list, blank=True)
    # Conservative scaffold default; the final business quota is configurable.
    quota_bytes_per_student = models.PositiveBigIntegerField(default=10_000_000_000)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(deadline__gt=F("opens_at")), name="activity_positive_window"),
            models.CheckConstraint(condition=Q(quota_bytes_per_student__gt=0), name="activity_positive_quota"),
        ]
        ordering = ["-created_at"]

    def clean(self):
        if not isinstance(self.allowed_classes, list) or any(not isinstance(x, str) for x in self.allowed_classes):
            raise ValidationError({"allowed_classes": "必须填写班级名称的 JSON 列表。"})

    def __str__(self):
        return self.title


class MaterialRequirement(BaseModel):
    activity = models.ForeignKey(Activity, on_delete=models.PROTECT, related_name="requirements")
    code = models.SlugField(max_length=64)
    title = models.CharField(max_length=100)
    required = models.BooleanField(default=True)
    allowed_extensions = models.JSONField(default=list)
    max_file_bytes = models.PositiveBigIntegerField(default=10_000_000_000)
    max_count = models.PositiveIntegerField(default=1)
    rule_version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["activity", "code"], name="unique_activity_material_code"),
            models.CheckConstraint(condition=Q(max_file_bytes__gt=0) & Q(max_file_bytes__lte=10_000_000_000), name="material_file_cap"),
            models.CheckConstraint(condition=Q(max_count__gt=0), name="material_positive_count"),
        ]

    def clean(self):
        extensions = self.allowed_extensions
        if not isinstance(extensions, list) or not extensions or any(
            not isinstance(x, str) or not x.startswith(".") or x != x.lower() or "/" in x or "\\" in x
            for x in extensions
        ):
            raise ValidationError({"allowed_extensions": '例如 [".pdf", ".docx"]，不能为空。'})
