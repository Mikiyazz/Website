from django.conf import settings
from django.db import models
from apps.common.models import BaseModel


class AuditLog(BaseModel):
    """安全审计流水：登录态操作、越权尝试、拒绝动作都落这张表。

    字段对齐审计口径：user(actor) / action / target / ip / reason / created_at。
    target 由 target_type + target_id 两个字段共同表达，metadata 里放上下文细节。
    """

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    action = models.CharField(max_length=100)
    target_type = models.CharField(max_length=100)
    target_id = models.CharField(max_length=64)
    request_id = models.CharField(max_length=32, blank=True)
    # 拒绝原因：越权 / 大小超限 / 魔数不符 / ZipSlip 等，直接落可读中文，便于管理端筛选。
    reason = models.CharField(max_length=300, blank=True)
    # 来源 IP：默认只信任 REMOTE_ADDR，只有显式开启 TRUST_X_FORWARDED_FOR 才读代理头。
    ip = models.GenericIPAddressField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["target_type", "target_id", "created_at"]),
            models.Index(fields=["action", "created_at"]),
            models.Index(fields=["actor", "created_at"]),
        ]

    def __str__(self):
        return f"{self.action} @ {self.created_at:%Y-%m-%d %H:%M:%S}"
