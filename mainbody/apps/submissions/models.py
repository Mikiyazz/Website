from django.conf import settings
from django.db import models
from apps.common.models import BaseModel


class Submission(BaseModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        SUBMITTED = "submitted", "待审核"
        APPROVED = "approved", "已通过"
        RETURNED = "returned", "退回修改"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    activity = models.ForeignKey("activities.Activity", on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    draft_data = models.JSONField(default=dict, blank=True)
    version = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "activity"], name="unique_student_activity_submission")]
        indexes = [models.Index(fields=["activity", "status", "created_at"])]


class SubmissionRevision(BaseModel):
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="revisions")
    number = models.PositiveIntegerField()
    snapshot = models.JSONField()
    request_key = models.UUIDField(unique=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["submission", "number"], name="unique_submission_revision")]
