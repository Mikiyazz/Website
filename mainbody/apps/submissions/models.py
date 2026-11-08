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

    # 审核工作台徽章语义（任务约定）：草稿 / 已提交 / 复审中 / 通过 / 驳回 / 迟交。
    # LATE_SUBMITTED 不是独立状态机节点，而是“已提交但命中补交白名单”的徽章口径，
    # 数据库状态仍走 pending_first_review，避免把审核流转拆成两套。
    LATE_SUBMITTED = "late_submitted"
    BADGE_ORDER = (
        (Status.DRAFT, "草稿", "gray"),
        (Status.PENDING_FIRST_REVIEW, "已提交", "blue"),
        (Status.PENDING_FINAL_REVIEW, "复审中", "yellow"),
        (Status.FINAL_APPROVED, "通过", "green"),
        (Status.RETURNED, "驳回", "red"),
        (LATE_SUBMITTED, "迟交已提交", "purple"),
    )

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    activity = models.ForeignKey("activities.Activity", on_delete=models.PROTECT)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    draft_data = models.JSONField(default=dict, blank=True)
    version = models.PositiveIntegerField(default=0)
    # 命中补交白名单且在截止后提交：导出归档时文件名前加 late_ 前缀，方便老师区分。
    is_late_submission = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "activity"], name="unique_student_activity_submission")]
        indexes = [models.Index(fields=["activity", "status", "created_at"])]

    @property
    def badge_status(self) -> str:
        """Badge code used by the review workbench (LATE_SUBMITTED overrides 已提交)."""
        if self.is_late_submission and self.status in (
            self.Status.PENDING_FIRST_REVIEW,
            self.Status.PENDING_FINAL_REVIEW,
            self.Status.DRAFT,
        ):
            return self.LATE_SUBMITTED
        return self.status

    @property
    def badge_label(self) -> str:
        labels = {code: label for code, label, _ in self.BADGE_ORDER}
        return labels.get(self.badge_status, self.get_status_display())

    @property
    def badge_tone(self) -> str:
        tones = {code: tone for code, _, tone in self.BADGE_ORDER}
        return tones.get(self.badge_status, "gray")

    @property
    def export_name_prefix(self) -> str:
        return "late_" if self.is_late_submission else ""


class SubmissionRevision(BaseModel):
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="revisions")
    number = models.PositiveIntegerField()
    snapshot = models.JSONField()
    request_key = models.UUIDField(unique=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["submission", "number"], name="unique_submission_revision")]
