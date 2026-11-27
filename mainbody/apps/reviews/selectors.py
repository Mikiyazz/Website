from django.core.paginator import Paginator
from django.db.models import Count, Max, Prefetch, Q
from apps.accounts.models import StudentRoster, User
from apps.accounts.permissions import (
    build_class_scope_q,
    format_class_scope_label,
    format_user_class_label,
    is_college_admin,
    is_reviewer,
    reviewer_allowed_class_labels,
    reviewer_own_class_scope,
    reviewer_user_scope_q,
)
from apps.activities.models import Activity
from apps.common.errors import DomainError
from apps.exports.services import resolve_status_filter
from apps.submissions.models import Submission
from apps.submissions.services import redundant_member_draft_ids
from .models import ReviewAssignment, ReviewRecord


def reviewable_submissions(user, activity_id):
    if is_college_admin(user):
        qs = Submission.objects.filter(activity_id=activity_id)
    elif not is_reviewer(user):
        return Submission.objects.none()
    else:
        qs = Submission.objects.filter(activity_id=activity_id).filter(
            reviewer_user_scope_q(user, prefix="owner__", activity_id=activity_id)
        )
    redundant_ids = redundant_member_draft_ids([activity_id])
    if redundant_ids:
        qs = qs.exclude(pk__in=redundant_ids)
    return qs


# =====================================================================
# 审核工作台（/reviews/workbench/）：服务端 Q 过滤 + 数据库聚合 + 分页 + 全历史时间线
# =====================================================================

WORKBENCH_PAGE_SIZE = 25
WORKBENCH_MAX_PAGE_SIZE = 50
WORKBENCH_ROW_LIMIT = 200


def parse_page_size(raw_value, *, default: int = WORKBENCH_PAGE_SIZE, maximum: int = WORKBENCH_MAX_PAGE_SIZE) -> int:
    try:
        parsed = int(str(raw_value).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    if parsed <= 0:
        return default
    return min(parsed, maximum)


def workbench_activities(user):
    """Activity dropdown scope: admin or class-bound reviewer sees all; assignment-only reviewer sees assigned activities."""
    if is_college_admin(user):
        return Activity.objects.all().order_by("-created_at")
    if not is_reviewer(user):
        return Activity.objects.none()
    if reviewer_own_class_scope(user) is not None:
        return Activity.objects.all().order_by("-created_at")
    assigned = ReviewAssignment.objects.filter(reviewer=user).exclude(class_name="")
    if not assigned.exists():
        return Activity.objects.none()
    return Activity.objects.filter(id__in=assigned.values("activity_id")).distinct().order_by("-created_at")


def workbench_permitted_submissions(user, activity_ids):
    """Base queryset with the same reviewer/class permission semantics as reviewable_submissions."""
    if not activity_ids:
        return Submission.objects.none()
    qs = Submission.objects.filter(activity_id__in=activity_ids)
    redundant_ids = redundant_member_draft_ids(activity_ids)
    if redundant_ids:
        qs = qs.exclude(pk__in=redundant_ids)
    if is_college_admin(user):
        return qs
    if not is_reviewer(user):
        return Submission.objects.none()
    if reviewer_own_class_scope(user) is not None:
        return qs.filter(reviewer_user_scope_q(user, prefix="owner__"))
    assignments = list(
        ReviewAssignment.objects.filter(reviewer=user, activity_id__in=activity_ids).values_list(
            "activity_id", "grade", "major", "class_name"
        )
    )
    clause = Q(pk__in=[])
    has_valid = False
    for activity_id, grade, major, class_name in assignments:
        clean_cls = str(class_name or "").strip()
        if not clean_cls:
            continue
        item_q = Q(activity_id=activity_id, owner__class_name=clean_cls)
        clean_grade = str(grade or "").strip()
        clean_major = str(major or "").strip()
        if clean_grade:
            item_q &= Q(owner__grade=clean_grade)
        if clean_major:
            item_q &= Q(owner__major=clean_major)
        clause |= item_q
        has_valid = True
    if not has_valid:
        return Submission.objects.none()
    return qs.filter(clause)


def workbench_class_options(user=None):
    """Class dropdown: admin sees all exact (grade, major, class_name) labels; reviewer sees only their own scope."""
    if user is not None and not is_college_admin(user):
        if is_reviewer(user):
            return reviewer_allowed_class_labels(user)
        return []
    roster_classes = {
        format_class_scope_label(g, m, c)
        for g, m, c in StudentRoster.objects.exclude(class_name="")
        .values_list("grade", "major", "class_name")
        .distinct()
    }
    submitted_classes = {
        format_class_scope_label(g, m, c)
        for g, m, c in Submission.objects.exclude(owner__class_name="")
        .values_list("owner__grade", "owner__major", "owner__class_name")
        .distinct()
    }
    return sorted(lbl for lbl in (roster_classes | submitted_classes) if lbl)


def normalize_declaration_type_filter(value: str | None) -> str:
    """规范化申报形式筛选参数，支持 individual/team 及中文别名。"""
    raw = str(value or "").strip().lower()
    if raw in ("individual", "个人", "个人申报"):
        return Submission.DeclarationType.INDIVIDUAL
    if raw in ("team", "团队", "团队申报"):
        return Submission.DeclarationType.TEAM
    return ""


def declaration_type_filter_q(declaration_type: str | None) -> Q | None:
    """构造个人/团队筛选 Q 条件：team 精确匹配，individual 匹配非 team（含缺省个人草稿）。"""
    normalized = normalize_declaration_type_filter(declaration_type)
    if normalized == Submission.DeclarationType.TEAM:
        return Q(draft_data__declaration_type=Submission.DeclarationType.TEAM)
    if normalized == Submission.DeclarationType.INDIVIDUAL:
        return (
            Q(draft_data__declaration_type=Submission.DeclarationType.INDIVIDUAL)
            | Q(draft_data__declaration_type__isnull=True)
            | ~Q(draft_data__has_key="declaration_type")
            | Q(draft_data__declaration_type="")
        )
    return None


def build_workbench_filter_q(
    *,
    activity_id=None,
    class_name="",
    grade="",
    major="",
    category="",
    declaration_type="",
    status="",
    late="",
    whitelist_numbers=None,
) -> Q:
    """Server-side filter composition; never trust client-side table filtering."""
    filter_q = Q()
    if activity_id:
        filter_q &= Q(activity_id=activity_id)
    class_q = build_class_scope_q(class_name, prefix="owner__", grade=grade, major=major)
    if class_q:
        filter_q &= class_q
    category = str(category or "").strip()
    if category:
        filter_q &= Q(draft_data__category=category)
    decl_q = declaration_type_filter_q(declaration_type)
    if decl_q is not None:
        filter_q &= decl_q

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


def summarize_submission_counts(qs, *, include_late: bool = False) -> dict[str, int]:
    """基于数据库 values('status').annotate(Count('id')) 的真实全量统计，避免拉全表或被分页截断。"""
    counts = {
        "total": 0,
        "pending_first_review": 0,
        "pending_final_review": 0,
        "final_approved": 0,
        "returned": 0,
        "draft": 0,
    }
    if include_late:
        counts["late"] = 0
    for row in qs.values("status").annotate(total=Count("id")):
        status_code = row["status"]
        cnt = int(row["total"] or 0)
        counts["total"] += cnt
        if status_code in counts:
            counts[status_code] = cnt
    if include_late:
        counts["late"] = qs.filter(is_late_submission=True).count()
    return counts


def _serialize_workbench_rows(user, submissions) -> list[dict]:
    rows = []
    for submission in submissions:
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
                "class_display": format_user_class_label(submission.owner) or "-",
                "grade": submission.owner.grade,
                "major": submission.owner.major,
                "activity_title": submission.activity.title,
                "project_name": (submission.draft_data or {}).get("project_name", "")
                if isinstance(submission.draft_data, dict)
                else "",
                "category": (submission.draft_data or {}).get("category", "")
                if isinstance(submission.draft_data, dict)
                else "",
                "declaration_type": (
                    Submission.DeclarationType.TEAM
                    if isinstance(submission.draft_data, dict)
                    and submission.draft_data.get("declaration_type") == Submission.DeclarationType.TEAM
                    else Submission.DeclarationType.INDIVIDUAL
                ),
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
    return rows


def collect_workbench_page(
    *,
    user,
    activity_id=None,
    class_name="",
    grade="",
    major="",
    category="",
    declaration_type="",
    status="",
    late="",
    whitelist_numbers=None,
    page=1,
    page_size=WORKBENCH_PAGE_SIZE,
):
    """Return paginated workbench rows, Page object, DB-aggregated counts, and activities."""
    activities = list(workbench_activities(user))
    activity_ids = [act.id for act in activities]
    base_qs = workbench_permitted_submissions(user, activity_ids)

    filter_q = build_workbench_filter_q(
        activity_id=activity_id,
        class_name=class_name,
        grade=grade,
        major=major,
        category=category,
        declaration_type=declaration_type,
        status=status,
        late=late,
        whitelist_numbers=whitelist_numbers,
    )
    filtered_qs = base_qs.filter(filter_q)
    counts = summarize_submission_counts(filtered_qs, include_late=True)

    resolved_page_size = parse_page_size(page_size)
    paginator = Paginator(
        filtered_qs.order_by("-updated_at", "-created_at").values_list("id", flat=True),
        resolved_page_size,
    )
    # 复用已聚合的真实总数，省去一次重复 COUNT(*) 查询。
    paginator._count = counts["total"]
    page_obj = paginator.get_page(page)
    page_ids = list(page_obj.object_list)

    if page_ids:
        page_subs_by_id = {
            sub.id: sub
            for sub in Submission.objects.filter(id__in=page_ids)
            .select_related("owner", "activity")
            .annotate(submitted_at=Max("revisions__created_at"))
            .prefetch_related(
                Prefetch(
                    "review_records",
                    queryset=ReviewRecord.objects.select_related("reviewer").order_by("created_at"),
                    to_attr="ordered_review_records",
                )
            )
        }
        ordered_subs = [page_subs_by_id[sid] for sid in page_ids if sid in page_subs_by_id]
        rows = _serialize_workbench_rows(user, ordered_subs)
    else:
        rows = []

    return {
        "rows": rows,
        "page_obj": page_obj,
        "counts": counts,
        "activities": activities,
        "page_size": resolved_page_size,
    }


def collect_workbench_rows(
    *,
    user,
    activity_id=None,
    class_name="",
    grade="",
    major="",
    category="",
    declaration_type="",
    status="",
    late="",
    whitelist_numbers=None,
    page=1,
    page_size=WORKBENCH_PAGE_SIZE,
):
    """Backward-compatible wrapper returning (rows, activities) for one page."""
    result = collect_workbench_page(
        user=user,
        activity_id=activity_id,
        class_name=class_name,
        grade=grade,
        major=major,
        category=category,
        declaration_type=declaration_type,
        status=status,
        late=late,
        whitelist_numbers=whitelist_numbers,
        page=page,
        page_size=page_size,
    )
    return result["rows"], result["activities"]


def workbench_whitelist_numbers(*, activities, activity_id=None):
    """Union of late whitelist numbers for the late filter (one activity when selected)."""
    numbers = set()
    for act in activities:
        if activity_id and str(act.id) != str(activity_id):
            continue
        numbers.update(str(sn).strip() for sn in (act.late_whitelist_student_numbers or []) if str(sn).strip())
    return sorted(numbers)
