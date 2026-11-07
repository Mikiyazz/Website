import re
import uuid
from django.http import JsonResponse
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from apps.accounts.permissions import is_college_admin, require_student
from apps.audit.services import record
from apps.common.api import endpoint, json_body
from apps.common.errors import DomainError, Forbidden
from apps.uploads.services import ensure_standard_material_requirements
from .models import Activity
from .selectors import eligible_activities


def _parse_dt(val, field_label: str):
    if not val:
        raise DomainError(f"缺少必要时间参数：{field_label}。")
    dt = parse_datetime(str(val).strip())
    if dt is None:
        raise DomainError(f"{field_label} 时间格式不正确，请使用 ISO 格式（如 2026-07-01T08:00）。")
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def serialize_activity(act: Activity):
    return {
        "id": str(act.id),
        "title": act.title,
        "opens_at": act.opens_at.isoformat(),
        "deadline": act.deadline.isoformat(),
        "is_open": act.is_open,
        "allowed_classes": list(act.allowed_classes or []),
        "late_whitelist_student_numbers": list(act.late_whitelist_student_numbers or []),
    }


@endpoint("GET")
def activity_list(request):
    if is_college_admin(request.user):
        qs = Activity.objects.all()
    else:
        require_student(request.user)
        qs = eligible_activities(request.user)
    rows = list(
        qs.values(
            "id",
            "title",
            "opens_at",
            "deadline",
            "is_open",
            "late_whitelist_student_numbers",
        )[:100]
    )
    return JsonResponse({"items": rows, "limit": 100})


@endpoint("POST")
def manage_activity(request):
    if not is_college_admin(request.user):
        raise Forbidden("仅学院管理员可配置活动时间与参数。")
    data = json_body(request)
    activity_id = data.get("activity_id") or data.get("id")
    title = str(data.get("title") or "").strip()
    opens_at_raw = data.get("opens_at")
    deadline_raw = data.get("deadline")
    is_open = bool(data.get("is_open", True))

    opens_at = _parse_dt(opens_at_raw, "开始时间 (opens_at)")
    deadline = _parse_dt(deadline_raw, "截止时间 (deadline)")
    if deadline <= opens_at:
        raise DomainError("活动截止时间必须晚于开始时间。")

    if activity_id:
        try:
            act_uuid = uuid.UUID(str(activity_id))
        except ValueError:
            raise DomainError("activity_id 必须为有效 UUID。") from None
        act = Activity.objects.get(pk=act_uuid)
        if title:
            act.title = title
        act.opens_at = opens_at
        act.deadline = deadline
        act.is_open = is_open
        if "late_whitelist_student_numbers" in data and isinstance(data["late_whitelist_student_numbers"], list):
            act.late_whitelist_student_numbers = [
                str(sn).strip() for sn in data["late_whitelist_student_numbers"] if str(sn).strip()
            ]
        act.save()
        action_name = "activity.updated"
    else:
        if not title:
            raise DomainError("新建活动必须填写活动名称。")
        whitelist = []
        if isinstance(data.get("late_whitelist_student_numbers"), list):
            whitelist = [str(sn).strip() for sn in data["late_whitelist_student_numbers"] if str(sn).strip()]
        act = Activity.objects.create(
            title=title,
            opens_at=opens_at,
            deadline=deadline,
            is_open=is_open,
            late_whitelist_student_numbers=whitelist,
        )
        action_name = "activity.created"

    ensure_standard_material_requirements(act)
    record(
        actor=request.user,
        action=action_name,
        target=act,
        metadata=serialize_activity(act),
    )
    return JsonResponse({"activity": serialize_activity(act)}, status=200)


@endpoint("POST")
def manage_whitelist(request, activity_id):
    if not is_college_admin(request.user):
        raise Forbidden("仅学院管理员可管理补交白名单。")
    data = json_body(request)
    act = Activity.objects.get(pk=activity_id)
    action = str(data.get("action") or "add").strip().lower()
    raw_numbers = data.get("student_numbers") or data.get("student_number") or ""

    if isinstance(raw_numbers, list):
        numbers = [str(x).strip() for x in raw_numbers if str(x).strip()]
    else:
        numbers = [x.strip() for x in re.split(r"[,，;；\s]+", str(raw_numbers)) if x.strip()]

    if not numbers:
        raise DomainError("请至少提供一个有效的学生学号。")

    if action == "remove":
        updated_list = act.remove_from_late_whitelist(*numbers)
    elif action == "add":
        updated_list = act.add_to_late_whitelist(*numbers)
    else:
        raise DomainError("action 参数仅支持 add 或 remove。")

    record(
        actor=request.user,
        action=f"activity.whitelist.{action}",
        target=act,
        metadata={"student_numbers": numbers, "late_whitelist_student_numbers": updated_list},
    )
    return JsonResponse(
        {
            "activity_id": str(act.id),
            "late_whitelist_student_numbers": updated_list,
        },
        status=200,
    )
