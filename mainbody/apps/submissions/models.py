from django.conf import settings
from django.db import models
from apps.common.models import BaseModel


class Submission(BaseModel):
    class DeclarationType(models.TextChoices):
        INDIVIDUAL = "individual", "个人"
        TEAM = "team", "团队"

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_FIRST_REVIEW = "pending_first_review", "待初审"
        RETURNED = "returned", "打回修改"
        PENDING_FINAL_REVIEW = "pending_final_review", "待终审"
        FINAL_APPROVED = "final_approved", "终审通过"

    Status.SUBMITTED = Status.PENDING_FIRST_REVIEW
    Status.APPROVED = Status.FINAL_APPROVED

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    activity = models.ForeignKey("activities.Activity", on_delete=models.PROTECT)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
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
