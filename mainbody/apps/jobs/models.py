from django.db import models
from django.utils import timezone
from apps.common.models import BaseModel


class OutboxEvent(BaseModel):
    class State(models.TextChoices):
        PENDING = "pending", "待处理"
        DONE = "done", "已处理"
        FAILED = "failed", "需排查"

    topic = models.CharField(max_length=100)
    dedup_key = models.CharField(max_length=200, unique=True)
    payload = models.JSONField(default=dict)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    lease_until = models.DateTimeField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=100, blank=True)

    class Meta:
        indexes = [models.Index(fields=["state", "available_at"])]
