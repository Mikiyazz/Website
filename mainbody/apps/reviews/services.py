"""Reviews service: multi-stage review state machine, permission check, and audit trail."""
import uuid
from django.db import transaction
from apps.accounts.models import User
from apps.accounts.permissions import is_college_admin, is_target_in_reviewer_scope
from apps.audit.services import record
from apps.common.errors import Conflict, DomainError, Forbidden
from apps.submissions.models import Submission
from .models import ReviewRecord


APPROVED_RETURN_STATUSES = frozenset({
    Submission.Status.PENDING_FINAL_REVIEW,
    Submission.Status.FINAL_APPROVED,
})


def has_first_review_permission(actor, activity_id, submission: Submission) -> bool:
    """判断操作者是否对该申报拥有初审或终审权限（直接基于审核员管辖范围判定，避免行锁事务内全表扫描）。"""
    if not getattr(actor, "is_authenticated", False) or not getattr(actor, "is_active", False):
        return False
    if submission is None:
        return False
    if activity_id is not None and str(submission.activity_id) != str(activity_id):
        return False
    if is_college_admin(actor):
        return True
    return is_target_in_reviewer_scope(actor, submission.owner, activity_id=activity_id)


@transaction.atomic
def review_submission(
    *,
    actor,
    activity_id,
    submission_id,
    decision,
    comment="",
    revision_id=None,
    request_key=None,
    confirm_approved_return: bool = False,
):
    if not actor.is_authenticated or not actor.is_active:
        raise Forbidden("请先登录有效账号。")

    if decision not in (ReviewRecord.Decision.APPROVED, ReviewRecord.Decision.RETURNED):
        raise DomainError("审核决定必须为 approved 或 returned。")

    comment = str(comment or "").strip()

    try:
        submission = (
            Submission.objects.select_for_update(of=("self",))
            .select_related("owner")
            .get(pk=submission_id, activity_id=activity_id)
        )
    except Submission.DoesNotExist:
        raise DomainError("未找到对应的提交记录。")

    latest_revision = submission.revisions.order_by("-number").first()
    if not latest_revision:
        raise Conflict("该提交尚未生成材料快照，无法进行审核。")

    if revision_id and str(latest_revision.id) != str(revision_id):
        raise Conflict("审核的目标版本已被更新，请刷新后重新审核。")

    current_status = submission.status
    actor_is_admin = is_college_admin(actor)
    actor_can_first = has_first_review_permission(actor, activity_id, submission)

    if current_status == Submission.Status.PENDING_FIRST_REVIEW:
        # 第一级：初审（团支书 / 审核人员，或学院管理员）
        if not actor_can_first:
            raise Forbidden("无权对此材料进行初审。")

        stage = ReviewRecord.Stage.FIRST

        if decision == ReviewRecord.Decision.RETURNED:
            if not comment:
                raise DomainError("初审打回时必须填写打回意见，原因不能为空。")
            next_status = Submission.Status.RETURNED
        else:
            # 初审通过 -> 待终审
            next_status = Submission.Status.PENDING_FINAL_REVIEW

    elif current_status == Submission.Status.PENDING_FINAL_REVIEW:
        if decision == ReviewRecord.Decision.APPROVED:
            # 终审通过仅限学院管理员/终审老师
            if not actor_is_admin:
                raise Forbidden("仅学院管理员/老师拥有终审通过权限。")
            stage = ReviewRecord.Stage.FINAL
            next_status = Submission.Status.FINAL_APPROVED
        else:
            # 已过初审的成果：拥有初审或终审权限的人员均可打回，但需填写意见并完成二次确认
            if not actor_can_first:
                raise Forbidden("无权打回此材料。")
            if not comment:
                raise DomainError("打回修改时必须填写打回意见，原因不能为空。")
            if not confirm_approved_return:
                raise DomainError("该材料已通过初审，打回修改前请先完成二次确认。")
            stage = ReviewRecord.Stage.FINAL if actor_is_admin else ReviewRecord.Stage.FIRST
            next_status = Submission.Status.RETURNED

    elif current_status == Submission.Status.FINAL_APPROVED:
        if decision == ReviewRecord.Decision.APPROVED:
            raise Conflict("该材料已终审通过，无需重复通过。")
        # 已终审通过的成果：拥有初审或终审权限的人员均可打回，但需填写意见并完成二次确认
        if not actor_can_first:
            raise Forbidden("无权打回此材料。")
        if not comment:
            raise DomainError("打回修改时必须填写打回意见，原因不能为空。")
        if not confirm_approved_return:
            raise DomainError("该成果已终审通过，打回修改前请先完成二次确认。")
        stage = ReviewRecord.Stage.FINAL if actor_is_admin else ReviewRecord.Stage.FIRST
        next_status = Submission.Status.RETURNED

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
            "previous_status": current_status,
            "status": submission.status,
            "comment": comment,
            "confirm_approved_return": bool(confirm_approved_return),
        },
    )

    return review_record, submission
