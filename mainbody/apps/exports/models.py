from django.conf import settings
from django.db import models
from apps.common.models import BaseModel


class ExportJob(BaseModel):
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    activity = models.ForeignKey("activities.Activity", on_delete=models.PROTECT)
    class Status(models.TextChoices):
        PENDING = "pending", "等待"
        RUNNING = "running", "进行中"
        DONE = "done", "完成"
        FAILED = "failed", "失败"

    status = models.CharField(max_length=20, default=Status.PENDING, choices=Status.choices)
    filters = models.JSONField(default=dict)
    result_key = models.CharField(max_length=255, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    # 异步导出进度：total/current 以“待写入 ZIP 的附件文件数”为单位，轮询端据此画进度条。
    total = models.PositiveIntegerField(default=0)
    current = models.PositiveIntegerField(default=0)
    file_path = models.CharField("MEDIA_ROOT 相对路径", max_length=500, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["activity", "status"]), models.Index(fields=["requested_by", "created_at"])]

    @property
    def percent(self) -> int:
        if self.status == self.Status.DONE:
            return 100
        if self.total <= 0:
            return 0
        return min(99, int(self.current * 100 / self.total))
