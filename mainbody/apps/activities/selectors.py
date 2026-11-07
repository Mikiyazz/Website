from django.db import connection
from django.db.models import Q
from django.utils import timezone
from apps.common.errors import Conflict, Forbidden
from .models import Activity


def eligible_activities(user):
    # 600 users is not a reason to read unlimited activities; callers paginate.
    if connection.vendor == "postgresql":
        return Activity.objects.filter(Q(allowed_classes=[]) | Q(allowed_classes__contains=[user.class_name]))
    student_number = str(getattr(user, "username", "") or "").strip()
    user_class = str(getattr(user, "class_name", "") or "").strip()
    matched_ids = [
        act.id
        for act in Activity.objects.all()[:200]
        if not act.allowed_classes
        or user_class in act.allowed_classes
        or (student_number and act.is_in_late_whitelist(student_number))
    ]
    return Activity.objects.filter(id__in=matched_ids)


def is_activity_open_for_user(activity: Activity, user) -> tuple[bool, str]:
    if activity.allowed_classes and user.class_name not in activity.allowed_classes:
        return False, "未获得参加此活动的权限。"
    now = timezone.now()
    if activity.is_open and (activity.opens_at <= now < activity.deadline):
        return True, "开放申报中"
    if now >= activity.deadline:
        student_number = str(getattr(user, "username", "") or "").strip()
        if student_number and activity.is_in_late_whitelist(student_number):
            return True, "补交白名单开放中"
        return False, "活动已截止"
    if not activity.is_open:
        return False, "活动暂未开放"
    return False, "尚未到达活动开始时间"


def ensure_activity_open(activity, user):
    if activity.allowed_classes and user.class_name not in activity.allowed_classes:
        raise Forbidden("未获得参加此活动的权限。")
    now = timezone.now()
    if activity.is_open and (activity.opens_at <= now < activity.deadline):
        return
    if now >= activity.deadline:
        student_number = str(getattr(user, "username", "") or "").strip()
        whitelist = {
            str(sn).strip()
            for sn in (getattr(activity, "late_whitelist_student_numbers", None) or [])
            if str(sn).strip()
        }
        if student_number and student_number in whitelist:
            return
    raise Conflict("活动尚未开放或已截止。")
