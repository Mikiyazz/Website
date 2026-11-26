"""可选新闻稿：学生自愿投稿 -> 团支书初审 -> 团委组织管理员终审。

与主申报（apps.submissions）的关系
--------------------------------
* 一份申报最多一篇新闻稿（OneToOne），新闻稿是"选交"材料，不参与必交校验。
* 正文文件本身走上传链路（requirement.code = "news_document"，只允许 .docx），
  这里只保存文字元数据与审核状态，不重复存文件字节。
* 版本语义：每次"提交/重新提交"version + 1，并写一条不可变的 NewsRevision 快照；
  submitted / reviewing 期间仍允许学生修改重提，审核老师按 reviewed_version 判断审的是哪一版。
* approved 之后锁定（EDITABLE_STATUSES 不含 APPROVED），只能由管理员退回再改。
"""
from django.db import models

from apps.common.models import BaseModel


class NewsDraft(BaseModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        SUBMITTED = "submitted", "待初审"
        REVIEWING = "reviewing", "待终审"
        APPROVED = "approved", "已采纳"
        RETURNED = "returned", "退回修改"

    # 允许学生保存/重新提交的状态白名单：已采纳后锁定。
    EDITABLE_STATUSES = frozenset(
        {Status.DRAFT, Status.RETURNED, Status.SUBMITTED, Status.REVIEWING}
    )

    # Excel 第 7 列（新闻稿状态）取值口径。
    EXCEL_LABELS = {
        Status.DRAFT: "草稿",
        Status.SUBMITTED: "待初审",
        Status.REVIEWING: "待终审",
        Status.APPROVED: "已采纳",
        Status.RETURNED: "退回修改",
    }
    NO_NEWS_LABEL = "未提交"

    # 徽章配色复用 static/css/app.css 的 .badge-<code>，与主申报保持同一套视觉口径。
    BADGE_ORDER = (
        (Status.DRAFT, "草稿", "draft"),
        (Status.SUBMITTED, "待初审", "pending_first_review"),
        (Status.REVIEWING, "待终审", "pending_final_review"),
        (Status.APPROVED, "已采纳", "final_approved"),
        (Status.RETURNED, "退回修改", "returned"),
    )

    submission = models.OneToOneField(
        "submissions.Submission",
        on_delete=models.PROTECT,
        related_name="news_draft",
    )
    title = models.CharField("标题", max_length=200, blank=True)
    body = models.TextField("备注", blank=True)
    version = models.PositiveIntegerField("版本号", default=0, editable=False)
    status = models.CharField(
        "状态", max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    returned_comment = models.TextField("退回意见", blank=True, default="")
    submitted_at = models.DateTimeField("最近一次提交时间", null=True, blank=True)
    # 审核老师"看过/处理过"的版本号：小于当前 version 说明学生已重新提交，需要复审。
    reviewed_version = models.PositiveIntegerField("已审核版本号", null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "updated_at"])]

    def __str__(self) -> str:  # pragma: no cover - 仅后台显示
        return f"{self.submission_id} / {self.title or '（未命名新闻稿）'}"

    @property
    def is_editable(self) -> bool:
        return self.status in self.EDITABLE_STATUSES

    @property
    def badge_status(self) -> str:
        return self.status

    @property
    def badge_label(self) -> str:
        return self.get_status_display()

    @property
    def badge_tone(self) -> str:
        return {code: tone for code, _, tone in self.BADGE_ORDER}.get(self.status, "draft")

    @property
    def excel_label(self) -> str:
        return self.EXCEL_LABELS.get(self.status, self.get_status_display())


class NewsRevision(BaseModel):
    """每次提交的不可变快照：'提交后仍能改' 的前提是旧版本有据可查。"""

    news = models.ForeignKey(NewsDraft, on_delete=models.PROTECT, related_name="revisions")
    number = models.PositiveIntegerField("版本序号")
    snapshot = models.JSONField("快照")
    request_key = models.UUIDField("幂等键", unique=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["news", "number"], name="unique_news_revision")
        ]
        ordering = ["news", "number"]
