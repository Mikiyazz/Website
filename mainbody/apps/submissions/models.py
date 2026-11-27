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
    # 假期前项目立项申报（免审核，提交即完成备案）；与下学期成果收集审核状态相互独立。
    declaration_submitted = models.BooleanField("已完成项目申报", default=False)
    declaration_submitted_at = models.DateTimeField("项目申报提交时间", null=True, blank=True)
    # 命中补交白名单且在截止后提交：导出归档时文件名前加 late_ 前缀，方便老师区分。
    is_late_submission = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["activity", "status", "created_at"]),
            models.Index(fields=["owner", "activity", "updated_at"]),
        ]

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

    def save(self, *args, **kwargs):
        is_create = self._state.adding
        update_fields = kwargs.get("update_fields")
        super().save(*args, **kwargs)
        # 仅当调用方未在 service 层显式同步、且新建非空草稿或显式修改 draft_data 时自动同步团队成员索引表。
        # 状态变更（如 review_submission 的 update_fields=["status", "updated_at"]）与新建空白草稿 0 额外 SQL。
        if getattr(self, "_skip_team_member_sync", False):
            return
        should_sync = (
            (is_create and isinstance(self.draft_data, dict) and bool(self.draft_data.get("team_members")))
            or (not is_create and (update_fields is None or "draft_data" in update_fields))
        )
        if should_sync:
            from .services import sync_submission_team_members

            sync_submission_team_members(self, is_create=is_create)


class SubmissionTeamMember(BaseModel):
    """团队申报成员索引表：替代遍历全表 draft_data JSON，支持毫秒级双向索引查询。"""

    submission = models.ForeignKey(
        Submission,
        on_delete=models.CASCADE,
        related_name="team_member_rows",
    )
    activity = models.ForeignKey(
        "activities.Activity",
        on_delete=models.CASCADE,
        related_name="submission_team_members",
    )
    student_number = models.CharField("成员学号", max_length=64)
    real_name = models.CharField("成员姓名", max_length=100, blank=True, default="")
    class_name = models.CharField("成员班级", max_length=100, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "student_number"],
                name="unique_submission_team_member",
            ),
        ]
        indexes = [
            models.Index(fields=["student_number", "activity"], name="sub_tm_stu_act_idx"),
            models.Index(fields=["activity", "student_number"], name="sub_tm_act_stu_idx"),
        ]


class SubmissionRevision(BaseModel):
    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="revisions")
    number = models.PositiveIntegerField()
    snapshot = models.JSONField()
    request_key = models.UUIDField(unique=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["submission", "number"], name="unique_submission_revision")]
