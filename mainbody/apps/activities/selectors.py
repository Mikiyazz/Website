from django.db import connection
from django.db.models import Q
from django.utils import timezone
from apps.accounts.permissions import is_college_admin
from apps.common.errors import Conflict, Forbidden
from .models import Activity


def eligible_activities(user):
    # 600 users is not a reason to read unlimited activities; callers paginate.
    is_admin = is_college_admin(user)
    user_class = str(getattr(user, "class_name", "") or "").strip()
    student_number = str(getattr(user, "username", "") or "").strip()
    if connection.vendor == "postgresql":
        if is_admin and not user_class:
            return Activity.objects.all()
        clause = Q(allowed_classes=[])
        if user_class:
            clause |= Q(allowed_classes__contains=[user_class])
        if student_number:
            clause |= Q(late_whitelist_student_numbers__contains=[student_number])
        return Activity.objects.filter(clause)
    matched_ids = [
        act.id
        for act in Activity.objects.all()[:200]
        if not act.allowed_classes
        or (user_class and user_class in act.allowed_classes)
        or (student_number and act.is_in_late_whitelist(student_number))
        or (is_admin and not user_class)
    ]
    return Activity.objects.filter(id__in=matched_ids)


def is_activity_open_for_user(activity: Activity, user) -> tuple[bool, str]:
    is_admin = is_college_admin(user)
    user_class = str(getattr(user, "class_name", "") or "").strip()
    student_number = str(getattr(user, "username", "") or "").strip()
    now = timezone.now()
    in_late_whitelist = bool(
        now >= activity.deadline
        and student_number
        and activity.is_in_late_whitelist(student_number)
        and activity.late_submission_allowed_at(now)
    )
    if in_late_whitelist:
        return True, "补交白名单开放中"
    if activity.allowed_classes and user_class not in activity.allowed_classes:
        if not (is_admin and not user_class):
            return False, "未获得参加此活动的权限。"
    if activity.is_open and (activity.opens_at <= now < activity.deadline):
        return True, "开放申报中"
    if now >= activity.deadline:
        return False, "活动已截止"
    if not activity.is_open:
        return False, "活动暂未开放"
    return False, "尚未到达活动开始时间"


def ensure_activity_open(activity, user):
    is_admin = is_college_admin(user)
    user_class = str(getattr(user, "class_name", "") or "").strip()
    student_number = str(getattr(user, "username", "") or "").strip()
    now = timezone.now()
    if (
        now >= activity.deadline
        and student_number
        and activity.is_in_late_whitelist(student_number)
        and activity.late_submission_allowed_at(now)
    ):
        return
    if activity.allowed_classes and user_class not in activity.allowed_classes:
        if not (is_admin and not user_class):
            raise Forbidden("未获得参加此活动的权限。")
    if activity.is_open and (activity.opens_at <= now < activity.deadline):
        return
    raise Conflict("活动尚未开放或已截止。")
