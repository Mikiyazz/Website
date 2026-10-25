from django.db import models
from apps.common.models import BaseModel


class ValidationResult(BaseModel):
    file = models.ForeignKey("uploads.FileAsset", on_delete=models.PROTECT, related_name="findings")
    rule_code = models.CharField(max_length=100)
    rule_version = models.PositiveIntegerField()
    outcome = models.CharField(max_length=20, choices=[("pass", "通过"), ("fail", "失败"), ("manual", "需人工")])
    location = models.CharField(max_length=200, blank=True)
    message = models.TextField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["file", "rule_code", "rule_version"], name="unique_file_rule_result")]
