from django.db.models import Max, Prefetch, Q
from apps.accounts.models import StudentRoster, User
from apps.accounts.permissions import is_college_admin
from apps.activities.models import Activity
from apps.common.errors import DomainError
from apps.exports.services import resolve_status_filter
from apps.submissions.models import Submission
from .models import ReviewAssignment, ReviewRecord


def reviewable_submissions(user, activity_id):
    if is_college_admin(user):
        return Submission.objects.filter(activity_id=activity_id)
    if not user.is_authenticated or not user.is_active or user.role != User.Role.REVIEWER:
        return Submission.objects.none()
    assignments = list(
        ReviewAssignment.objects.filter(reviewer=user, activity_id=activity_id).values_list("class_name", flat=True)
    )
    if not assignments or "" in assignments:
        return Submission.objects.filter(activity_id=activity_id)
    allowed_classes = [c.strip() for c in assignments if c and c.strip()]
    return Submission.objects.filter(activity_id=activity_id, owner__class_name__in=allowed_classes)

# =====================================================================
# 审核工作台（/reviews/workbench/）：服务端 Q 过滤 + 全历史时间线
# =====================================================================

WORKBENCH_ROW_LIMIT = 200


def workbench_activities(user):
    """Activity dropdown scope: admin sees all; reviewer sees assigned activities (or all)."""
    if is_college_admin(user):
        return Activity.objects.all().order_by("-created_at")
    if not getattr(user, "is_authenticated", False) or not user.is_active or user.role != User.Role.REVIEWER:
        return Activity.objects.none()
    assigned = ReviewAssignment.objects.filter(reviewer=user)
    if not assigned.exists():
        return Activity.objects.all().order_by("-created_at")
    return Activity.objects.filter(id__in=assigned.values("activity_id")).distinct().order_by("-created_at")


def workbench_permitted_submissions(user, activity_ids):
    """Base queryset with the same reviewer/class permission semantics as reviewable_submissions."""
    if not activity_ids:
        return Submission.objects.none()
    qs = Submission.objects.filter(activity_id__in=activity_ids)
    if is_college_admin(user):
        return qs
    if not getattr(user, "is_authenticated", False) or not user.is_active or user.role != User.Role.REVIEWER:
        return Submission.objects.none()
    assignments = list(
        ReviewAssignment.objects.filter(reviewer=user, activity_id__in=activity_ids).values_list(
            "activity_id", "class_name"
        )
    )
    if not assignments:
        return qs
    clause = Q(pk__in=[])
    for activity_id, class_name in assignments:
        clean = str(class_name or "").strip()
        if clean:
            clause |= Q(activity_id=activity_id, owner__class_name=clean)
        else:
            clause |= Q(activity_id=activity_id)
    return qs.filter(clause)


def workbench_class_options():
    """Class dropdown: deduplicated StudentRoster classes plus classes that already submitted."""
    roster_classes = set(
        StudentRoster.objects.exclude(class_name="").order_by("class_name").values_list("class_name", flat=True).distinct()
    )
    submitted_classes = set(
        Submission.objects.exclude(owner__class_name="").values_list("owner__class_name", flat=True).distinct()
    )
    return sorted(roster_classes | submitted_classes)


def build_workbench_filter_q(
    *,
    activity_id=None,
    class_name="",
    status="",
    late="",
    whitelist_numbers=None,
) -> Q:
    """Server-side filter composition; never trust client-side table filtering."""
    filter_q = Q()
    if activity_id:
        filter_q &= Q(activity_id=activity_id)
    class_name = str(class_name or "").strip()
    if class_name:
        filter_q &= Q(owner__class_name=class_name)

    status = str(status or "").strip()
    if status == Submission.LATE_SUBMITTED:
        filter_q &= Q(is_late_submission=True) & Q(
            status__in=[
                Submission.Status.DRAFT,
                Submission.Status.PENDING_FIRST_REVIEW,
                Submission.Status.PENDING_FINAL_REVIEW,
            ]
        )
    elif status:
        # 无法识别的状态参数不抛 500：只当作"未筛选"，避免手工改 URL 打挂页面。
        try:
            resolved_status = resolve_status_filter(status)
        except DomainError:
            resolved_status = None
        if resolved_status:
            filter_q &= Q(status=resolved_status)

    late = str(late or "").strip()
    if late in ("1", "0"):
        numbers = list(whitelist_numbers or [])
        if late == "1":
            filter_q &= Q(owner__username__in=numbers)
        else:
            filter_q &= ~Q(owner__username__in=numbers)
    return filter_q


def collect_workbench_rows(*, user, activity_id=None, class_name="", status="", late="", whitelist_numbers=None):
    """Return serialized workbench rows: student, activity, submitted_at, badge, full review history."""
    activities = list(workbench_activities(user))
    activity_ids = [act.id for act in activities]
    base_qs = workbench_permitted_submissions(user, activity_ids)

    filter_q = build_workbench_filter_q(
        activity_id=activity_id,
        class_name=class_name,
        status=status,
        late=late,
        whitelist_numbers=whitelist_numbers,
    )
    qs = (
        base_qs.filter(filter_q)
        .select_related("owner", "activity")
        .annotate(submitted_at=Max("revisions__created_at"))
        .prefetch_related(
            Prefetch(
                "review_records",
                queryset=ReviewRecord.objects.select_related("reviewer").order_by("created_at"),
                to_attr="ordered_review_records",
            )
        )
        .order_by("-updated_at")[:WORKBENCH_ROW_LIMIT]
    )

    rows = []
    for submission in qs:
        history = []
        for record in getattr(submission, "ordered_review_records", []):
            history.append(
                {
                    "reviewer": record.reviewer.real_name or record.reviewer.username,
                    "reviewer_stage": record.get_stage_display(),
                    "decision": record.get_decision_display(),
                    "decision_code": record.decision,
                    "comment": record.comment,
                    "created_at": record.created_at,
                }
            )
        rows.append(
            {
                "id": str(submission.id),
                "student_number": submission.owner.username,
                "real_name": submission.owner.real_name,
                "class_name": submission.owner.class_name,
                "activity_title": submission.activity.title,
                "project_name": (submission.draft_data or {}).get("project_name", "")
                if isinstance(submission.draft_data, dict)
                else "",
                "submitted_at": submission.submitted_at or submission.created_at,
                "status_code": submission.status,
                "badge_status": submission.badge_status,
                "badge_label": submission.badge_label,
                "badge_tone": submission.badge_tone,
                "is_late": submission.is_late_submission,
                "can_first_review": submission.status == Submission.Status.PENDING_FIRST_REVIEW
                and (is_college_admin(user) or user.role == User.Role.REVIEWER),
                "can_final_review": submission.status == Submission.Status.PENDING_FINAL_REVIEW
                and is_college_admin(user),
                "history": history,
                "detail_url": f"/reviews/submissions/{submission.id}/",
            }
        )
    return rows, activities


def workbench_whitelist_numbers(*, activities, activity_id=None):
    """Union of late whitelist numbers for the late filter (one activity when selected)."""
    numbers = set()
    for act in activities:
        if activity_id and str(act.id) != str(activity_id):
            continue
        numbers.update(str(sn).strip() for sn in (act.late_whitelist_student_numbers or []) if str(sn).strip())
    return sorted(numbers)
