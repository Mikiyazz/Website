from django.conf import settings
from django.db import models
from apps.common.models import BaseModel


class ExportJob(BaseModel):
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    activity = models.ForeignKey("activities.Activity", on_delete=models.PROTECT)
    status = models.CharField(max_length=20, default="pending", choices=[
        ("pending", "等待"), ("running", "进行中"), ("done", "完成"), ("failed", "失败")])
    filters = models.JSONField(default=dict)
    result_key = models.CharField(max_length=255, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
