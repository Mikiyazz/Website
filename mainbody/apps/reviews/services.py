"""Reviews service: multi-stage review state machine, permission check, and audit trail."""
import uuid
from django.db import transaction
from apps.accounts.models import User
from apps.accounts.permissions import is_college_admin
from apps.audit.services import record
from apps.common.errors import Conflict, DomainError, Forbidden
from apps.submissions.models import Submission
from .models import ReviewRecord
from .selectors import reviewable_submissions


@transaction.atomic
def review_submission(*, actor, activity_id, submission_id, decision, comment="", revision_id=None, request_key=None):
    if not actor.is_authenticated or not actor.is_active:
        raise Forbidden("请先登录有效账号。")

    if decision not in (ReviewRecord.Decision.APPROVED, ReviewRecord.Decision.RETURNED):
        raise DomainError("审核决定必须为 approved 或 returned。")

    comment = str(comment or "").strip()

    try:
        submission = Submission.objects.select_for_update().get(pk=submission_id, activity_id=activity_id)
    except Submission.DoesNotExist:
        raise DomainError("未找到对应的提交记录。")

    latest_revision = submission.revisions.order_by("-number").first()
    if not latest_revision:
        raise Conflict("该提交尚未生成材料快照，无法进行审核。")

    if revision_id and str(latest_revision.id) != str(revision_id):
        raise Conflict("审核的目标版本已被更新，请刷新后重新审核。")

    current_status = submission.status

    if current_status == Submission.Status.PENDING_FIRST_REVIEW:
        # 第一级：初审（团支书 / 审核人员，或学院管理员）
        can_first_review = is_college_admin(actor) or (
            actor.role == User.Role.REVIEWER
            and reviewable_submissions(actor, activity_id).filter(pk=submission.pk).exists()
        )
        if not can_first_review:
            raise Forbidden("无权对此材料进行初审。")

        stage = ReviewRecord.Stage.FIRST

        if decision == ReviewRecord.Decision.RETURNED:
            # 强制校验：打回时必须填写打回意见，原因不能为空
            if not comment:
                raise DomainError("初审打回时必须填写打回意见，原因不能为空。")
            next_status = Submission.Status.RETURNED
        else:
            # 初审通过 -> 待终审
            next_status = Submission.Status.PENDING_FINAL_REVIEW

    elif current_status == Submission.Status.PENDING_FINAL_REVIEW:
        # 第二级：终审（老师 / 学院管理员）
        if not is_college_admin(actor):
            raise Forbidden("仅学院管理员/老师拥有终审权限。")

        stage = ReviewRecord.Stage.FINAL

        if decision == ReviewRecord.Decision.RETURNED:
            # 强制校验：打回时必须填写打回意见，原因不能为空
            if not comment:
                raise DomainError("终审打回时必须填写打回意见，原因不能为空。")
            next_status = Submission.Status.RETURNED
        else:
            # 终审通过
            next_status = Submission.Status.FINAL_APPROVED

    else:
        raise Conflict(f"当前材料状态为【{submission.get_status_display()}】，不在可审核流转阶段。")

    # 更新 Submission 状态
    submission.status = next_status
    submission.save(update_fields=["status", "updated_at"])

    # 记录不可变审核历史
    review_record = ReviewRecord.objects.create(
        submission=submission,
        revision=latest_revision,
        reviewer=actor,
        stage=stage,
        decision=decision,
        comment=comment,
        request_key=request_key or uuid.uuid4(),
    )

    # 审计记录
    record(
        actor=actor,
        action=f"review.{stage}.{decision}",
        target=submission,
        metadata={
            "review_record_id": str(review_record.id),
            "stage": stage,
            "decision": decision,
            "status": submission.status,
            "comment": comment,
        },
    )

    return review_record, submission
