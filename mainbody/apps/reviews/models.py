import uuid
from django.conf import settings
from django.db import models
from apps.common.models import BaseModel


class ReviewAssignment(BaseModel):
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    activity = models.ForeignKey("activities.Activity", on_delete=models.PROTECT)
    class_name = models.CharField(max_length=100)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["reviewer", "activity", "class_name"], name="unique_review_assignment")]


class ReviewRecord(BaseModel):
    class Stage(models.TextChoices):
        FIRST = "first_review", "初审"
        FINAL = "final_review", "终审"

    class Decision(models.TextChoices):
        APPROVED = "approved", "通过"
        RETURNED = "returned", "打回修改"

    submission = models.ForeignKey("submissions.Submission", on_delete=models.PROTECT, related_name="review_records", null=True, blank=True)
    revision = models.ForeignKey("submissions.SubmissionRevision", on_delete=models.PROTECT, related_name="review_records")
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.FIRST)
    decision = models.CharField(max_length=20, choices=Decision.choices)
    comment = models.TextField("审核意见", blank=True)
    request_key = models.UUIDField(default=uuid.uuid4, unique=True)
