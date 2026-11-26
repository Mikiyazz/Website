"""新闻稿服务层：短事务 + 行锁，业务状态变化只在这里发生。

规则（与方案定稿一致）
--------------------
1. 只能改自己申报的新闻稿；他人 id 一律 404，不泄露是否存在。
2. 可编辑状态：draft / returned / submitted / reviewing。approved 之后锁定。
3. 提交（含重新提交）后 version + 1，并写一条不可变 NewsRevision 快照；
   submitted / reviewing 期间重提不降级为草稿，仍留在 submitted 等待审理。
4. 退回必须带意见；终审只有学院管理员能做，初审只有被分配的团支书能做。
5. 新闻稿必须在主申报已正式提交之后才能提交（没做实践没法发稿）。
"""
import copy
import logging
import uuid
from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.permissions import is_college_admin, require_student
from apps.activities.selectors import ensure_activity_open
from apps.audit.services import record
from apps.common.errors import Conflict, DomainError, Forbidden
from apps.submissions.models import Submission
from apps.uploads.models import FileAsset, UploadSession
from apps.uploads.services import ensure_standard_material_requirements

from .models import NewsDraft, NewsRevision
from .selectors import news_belongs_to_reviewer

logger = logging.getLogger(__name__)

TITLE_MAX_LENGTH = 200
BODY_MAX_LENGTH = 20_000
COMMENT_MAX_LENGTH = 2_000

# 主申报进入这些状态后才允许提交新闻稿：草稿阶段还没"做过实践"。
SUBMISSION_SUBMITABLE_STATUSES = frozenset(
    {
        Submission.Status.PENDING_FIRST_REVIEW,
        Submission.Status.PENDING_FINAL_REVIEW,
        Submission.Status.FINAL_APPROVED,
        Submission.Status.RETURNED,
    }
)


def _parse_uuid(value, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise DomainError(f"{label} 必须为有效 UUID。") from None


def _clean_text(value, *, label: str, max_length: int, allow_empty: bool = True) -> str:
    text = str(value or "").strip()
    if not allow_empty and not text:
        raise DomainError(f"{label}不能为空。")
    if len(text) > max_length:
        raise DomainError(f"{label}不能超过 {max_length} 个字符。")
    return text


def _normalize_request_key(request_key) -> uuid.UUID:
    if request_key in (None, ""):
        return uuid.uuid4()
    return _parse_uuid(request_key, "request_key")


def _assert_editable(news: NewsDraft) -> None:
    if news.status == NewsDraft.Status.APPROVED:
        raise Forbidden("新闻稿已被采纳并锁定，如需修改请联系团委老师退回。")
    if news.status not in NewsDraft.EDITABLE_STATUSES:
        raise Conflict(f"当前新闻稿状态为【{news.get_status_display()}】，不允许此操作。")


def current_document(submission: Submission):
    """取该申报当前有效的新闻稿 Word（只认检查通过的上传记录）。"""
    if submission is None:
        return None
    return (
        UploadSession.objects.select_related("requirement", "fileasset")
        .filter(
            owner_id=submission.owner_id,
            requirement__activity_id=submission.activity_id,
            requirement__code="news_document",
            status=UploadSession.Status.COMPLETE,
            fileasset__status=FileAsset.Status.VALID,
        )
        .order_by("-created_at", "-id")
        .first()
    )


def _snapshot(news: NewsDraft, document: UploadSession | None) -> dict:
    """快照里只放元数据，不放文件字节，也不复制可被覆盖的本地路径。"""
    asset = getattr(document, "fileasset", None) if document is not None else None
    return {
        "title": news.title,
        "body": news.body,
        "status": news.status,
        "version": news.version,
        "document": (
            {
                "upload_id": str(document.pk),
                "original_name": document.original_name,
                "object_key": document.object_key,
                "declared_bytes": document.declared_bytes,
                "checksum_algorithm": getattr(asset, "checksum_algorithm", "") or "",
                "checksum_value": getattr(asset, "checksum_value", "") or "",
            }
            if document is not None
            else None
        ),
        "captured_at": timezone.now().isoformat(),
    }


@transaction.atomic
def get_or_create_news(*, actor, submission_id) -> NewsDraft:
    """打开（或首次创建）本人某份申报的新闻稿草稿，并确保活动材料规则已配置。"""
    require_student(actor)
    submission = (
        Submission.objects.select_related("activity", "owner")
        .select_for_update()
        .get(pk=_parse_uuid(submission_id, "submission_id"), owner=actor)
    )
    ensure_standard_material_requirements(submission.activity)

    news, created = NewsDraft.objects.get_or_create(
        submission=submission,
        defaults={"title": "", "body": "", "status": NewsDraft.Status.DRAFT, "version": 0},
    )
    if created:
        record(actor=actor, action="news.created", target=news, metadata={"submission_id": str(submission.pk)})
    return news


@transaction.atomic
def save_news(*, actor, news_id, expected_version, title=None, body=None) -> NewsDraft:
    """保存草稿：行锁 + expected_version 乐观锁，陈旧版本返回 409。"""
    require_student(actor)
    if type(expected_version) is not int or expected_version < 0:
        raise DomainError("expected_version 必须为非负整数。")

    news = (
        NewsDraft.objects.select_for_update()
        .select_related("submission", "submission__activity")
        .get(pk=_parse_uuid(news_id, "news_id"), submission__owner=actor)
    )
    _assert_editable(news)
    if news.version != expected_version:
        raise Conflict("新闻稿已被其他页面更新，请重新获取后再保存。")

    if title is not None:
        news.title = _clean_text(title, label="标题", max_length=TITLE_MAX_LENGTH)
    if body is not None:
        news.body = _clean_text(body, label="备注", max_length=BODY_MAX_LENGTH)

    news.version += 1
    news.save(update_fields=["title", "body", "version", "updated_at"])
    record(
        actor=actor,
        action="news.saved",
        target=news,
        metadata={"version": news.version, "status": news.status},
    )
    return news


@transaction.atomic
def submit_news(*, actor, news_id, request_key=None) -> tuple[NewsDraft, NewsRevision]:
    """提交或重新提交：校验主申报状态与 Word 附件，写版本快照。"""
    require_student(actor)
    news = (
        NewsDraft.objects.select_for_update()
        .select_related("submission", "submission__activity", "submission__owner")
        .get(pk=_parse_uuid(news_id, "news_id"), submission__owner=actor)
    )
    _assert_editable(news)

    submission = news.submission
    # 新闻稿必须在申报正式提交之后：草稿阶段还没有实践内容，不能发稿。
    if submission.status not in SUBMISSION_SUBMITABLE_STATUSES:
        raise Conflict("请先提交主申报，再提交新闻稿。")

    # 活动窗口：管理员不受限制，学生仍受截止与补交白名单约束。
    ensure_activity_open(submission.activity, actor)

    if not news.title:
        raise DomainError("标题不能为空。")
    if not news.body:
        raise DomainError("备注不能为空。")
    document = current_document(submission)
    if document is None:
        raise DomainError("请先上传新闻稿 Word 文件（仅支持 .docx）。")

    key = _normalize_request_key(request_key)
    if NewsRevision.objects.filter(request_key=key).exists():
        # 幂等重放：同一个 request_key 不重复生成快照，直接返回当前状态。
        news.refresh_from_db()
        return news, NewsRevision.objects.get(request_key=key)

    news.version += 1
    news.status = NewsDraft.Status.SUBMITTED
    news.submitted_at = timezone.now()
    news.save(update_fields=["version", "status", "submitted_at", "updated_at"])

    revision = NewsRevision.objects.create(
        news=news,
        number=news.version,
        snapshot=_snapshot(news, document),
        request_key=key,
    )
    record(
        actor=actor,
        action="news.submitted",
        target=news,
        metadata={
            "version": news.version,
            "revision_id": str(revision.pk),
            "has_document": bool(document),
            "document_name": document.original_name if document else "",
        },
    )
    return news, revision


@transaction.atomic
def review_news(*, actor, news_id, decision, comment="") -> NewsDraft:
    """两级审核：团支书初审（submitted -> reviewing），团委管理员终审（reviewing -> approved）。

    decision: "approve"（通过） / "return"（退回，必须带意见）。
    """
    if not getattr(actor, "is_authenticated", False) or not getattr(actor, "is_active", False):
        raise Forbidden("请先登录。")

    news = (
        NewsDraft.objects.select_for_update()
        .select_related("submission", "submission__activity", "submission__owner")
        .get(pk=_parse_uuid(news_id, "news_id"))
    )
    if not news_belongs_to_reviewer(actor, news):
        raise Forbidden("无权审核该新闻稿。")

    action = str(decision or "").strip().lower()
    if action not in ("approve", "return"):
        raise DomainError('decision 必须为 "approve" 或 "return"。')
    text = _clean_text(comment, label="审核意见", max_length=COMMENT_MAX_LENGTH)
    if action == "return" and not text:
        raise DomainError("退回时必须填写审核意见。")

    is_admin = is_college_admin(actor)

    if news.status == NewsDraft.Status.SUBMITTED:
        if action == "approve":
            news.status = NewsDraft.Status.REVIEWING
            outcome = "first_approved"
        else:
            news.status = NewsDraft.Status.RETURNED
            news.returned_comment = text
            outcome = "first_returned"
    elif news.status == NewsDraft.Status.REVIEWING:
        if not is_admin:
            raise Forbidden("终审仅限学院管理员操作。")
        if action == "approve":
            news.status = NewsDraft.Status.APPROVED
            outcome = "final_approved"
        else:
            news.status = NewsDraft.Status.RETURNED
            news.returned_comment = text
            outcome = "final_returned"
    else:
        raise Conflict(f"当前新闻稿状态为【{news.get_status_display()}】，不允许审核。")

    if action == "approve":
        news.returned_comment = ""
    news.reviewed_version = news.version
    news.save(update_fields=["status", "returned_comment", "reviewed_version", "updated_at"])
    record(
        actor=actor,
        action="news.reviewed",
        target=news,
        metadata={
            "outcome": outcome,
            "decision": action,
            "status": news.status,
            "reviewed_version": news.version,
            "comment": text[:300],
        },
    )
    return news


def serialize_news(news: NewsDraft) -> dict:
    document = current_document(news.submission)
    return {
        "id": str(news.pk),
        "submission_id": str(news.submission_id),
        "activity_id": str(news.submission.activity_id),
        "activity_title": news.submission.activity.title,
        "title": news.title,
        "body": news.body,
        "status": news.status,
        "status_display": news.get_status_display(),
        "badge": news.badge_status,
        "badge_tone": news.badge_tone,
        "version": news.version,
        "reviewed_version": news.reviewed_version,
        "returned_comment": news.returned_comment,
        "is_editable": news.is_editable,
        "submitted_at": _iso(news.submitted_at),
        "updated_at": _iso(news.updated_at),
        "document": (
            {
                "upload_id": str(document.pk),
                "original_name": document.original_name,
                "bytes": document.declared_bytes,
                "view_url": f"/api/v1/uploads/{document.pk}/file/?inline=1",
                "download_url": f"/api/v1/uploads/{document.pk}/file/",
                "delete_url": f"/api/v1/uploads/{document.pk}/delete/",
            }
            if document is not None
            else None
        ),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
