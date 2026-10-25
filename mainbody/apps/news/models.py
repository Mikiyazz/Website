from django.db import models
from apps.common.models import BaseModel


class NewsDraft(BaseModel):
    submission = models.OneToOneField("submissions.Submission", on_delete=models.PROTECT)
    title = models.CharField(max_length=200, blank=True)
    body = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=0)
