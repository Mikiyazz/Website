from django.utils import timezone
from apps.common.errors import Forbidden, Conflict
from .models import Activity


def eligible_activities(user):
    # 600 users is not a reason to read unlimited activities; callers paginate.
    from django.db.models import Q
    return Activity.objects.filter(Q(allowed_classes=[]) | Q(allowed_classes__contains=[user.class_name]))


def ensure_activity_open(activity, user):
    if activity.allowed_classes and user.class_name not in activity.allowed_classes:
        raise Forbidden("未获得参加此活动的权限。")
    now = timezone.now()
    if not activity.is_open or not (activity.opens_at <= now < activity.deadline):
        raise Conflict("活动尚未开放或已截止。")
