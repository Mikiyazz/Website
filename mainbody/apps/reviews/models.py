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
    revision = models.OneToOneField("submissions.SubmissionRevision", on_delete=models.PROTECT)
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    decision = models.CharField(max_length=20, choices=[("approved", "通过"), ("returned", "退回")])
    comment = models.TextField(blank=True)
    request_key = models.UUIDField(unique=True)
