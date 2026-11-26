from functools import wraps

from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import redirect

from apps.common.errors import Forbidden

from .models import User


STUDENT_ELIGIBLE_ROLES = frozenset({User.Role.STUDENT, User.Role.REVIEWER, User.Role.ADMIN, "grad_student"})


def require_student(user):
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        raise Forbidden("此操作需要登录有效账号。")
    if getattr(user, "role", "") not in STUDENT_ELIGIBLE_ROLES and not getattr(user, "is_superuser", False):
        raise Forbidden("此操作只对有效学生账号开放。")
    return True


def is_college_admin(user):
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and (getattr(user, "is_superuser", False) or user.role == User.Role.ADMIN)
    )


def is_reviewer(user):
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and user.role == User.Role.REVIEWER
    )


def format_user_class_label(user) -> str:
    """格式化用户的完整班级展示文本（年级 + 专业 + 班级，缺省自动省略）。"""
    if user is None:
        return ""
    grade = str(getattr(user, "grade", "") or "").strip()
    major = str(getattr(user, "major", "") or "").strip()
    class_name = str(getattr(user, "class_name", "") or "").strip()
    parts = [part for part in (grade, major, class_name) if part]
    return " ".join(parts)


def reviewer_own_class_scope(user) -> dict[str, str] | None:
    """返回团支书自身所属的精确班级范围；未配置班级时返回 None。"""
    if not is_reviewer(user):
        return None
    class_name = str(getattr(user, "class_name", "") or "").strip()
    if not class_name:
        return None
    return {
        "grade": str(getattr(user, "grade", "") or "").strip(),
        "major": str(getattr(user, "major", "") or "").strip(),
        "class_name": class_name,
    }


def reviewer_assigned_classes(user, *, activity_id=None) -> list[str]:
    """当团支书账号本身未填写班级时，读取显式配置的非空 ReviewAssignment 班级列表。"""
    if not is_reviewer(user):
        return []
    from apps.reviews.models import ReviewAssignment

    qs = ReviewAssignment.objects.filter(reviewer=user)
    if activity_id is not None:
        qs = qs.filter(activity_id=activity_id)
    classes = {
        str(cls or "").strip()
        for cls in qs.values_list("class_name", flat=True)
        if str(cls or "").strip()
    }
    return sorted(classes)


def reviewer_allowed_class_names(user, *, activity_id=None) -> list[str]:
    """返回团支书允许管理的 class_name 列表（默认拒绝：未绑定班级返回空列表）。"""
    scope = reviewer_own_class_scope(user)
    if scope is not None:
        return [scope["class_name"]]
    return reviewer_assigned_classes(user, activity_id=activity_id)


def reviewer_user_scope_q(user, *, prefix: str = "", activity_id=None) -> Q:
    """构造用于过滤学生或申报归属人的 Q 条件；管理员不限，团支书严格锁定本班，未绑定默认拒绝。"""
    if is_college_admin(user):
        return Q()
    if not is_reviewer(user):
        return Q(pk__in=[])

    scope = reviewer_own_class_scope(user)
    if scope is not None:
        clause = Q(**{f"{prefix}class_name": scope["class_name"]})
        if scope["grade"]:
            clause &= Q(**{f"{prefix}grade": scope["grade"]})
        if scope["major"]:
            clause &= Q(**{f"{prefix}major": scope["major"]})
        return clause

    assigned = reviewer_assigned_classes(user, activity_id=activity_id)
    if assigned:
        return Q(**{f"{prefix}class_name__in": assigned})
    return Q(pk__in=[])


def is_target_in_reviewer_scope(actor, target_user, *, activity_id=None) -> bool:
    """判断目标学生是否位于操作者（学院管理员或本班团支书）的管辖班级内。"""
    if is_college_admin(actor):
        return True
    if not is_reviewer(actor) or target_user is None:
        return False

    target_class = str(getattr(target_user, "class_name", "") or "").strip()
    if not target_class:
        return False

    scope = reviewer_own_class_scope(actor)
    if scope is not None:
        if target_class != scope["class_name"]:
            return False
        if scope["grade"] and str(getattr(target_user, "grade", "") or "").strip() != scope["grade"]:
            return False
        if scope["major"] and str(getattr(target_user, "major", "") or "").strip() != scope["major"]:
            return False
        return True

    assigned = reviewer_assigned_classes(actor, activity_id=activity_id)
    return target_class in assigned


def _audit_denial(request, required_label: str, reason: str) -> None:
    """Record a role-based denial without ever breaking the response."""
    try:
        from apps.audit.services import record_rejection

        record_rejection(
            action="access.role_denied",
            reason=reason,
            user=getattr(request, "user", None),
            target="",
            request=request,
            metadata={"required_role": required_label},
        )
    except Exception:  # pragma: no cover - 审计写失败不影响拒绝响应
        pass


def role_required(*roles, label: str = "此角色"):
    """视图级角色闸门：学生访问管理端/审核端一律 403，并写一条 access.role_denied 审计。"""

    allowed = frozenset(roles)
    display = label

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            user = getattr(request, "user", None)
            if user is None or not getattr(user, "is_authenticated", False):
                return redirect(f"/accounts/login/?next={request.path}")
            if not getattr(user, "is_active", False):
                _audit_denial(request, display, "账号已停用，无法访问该端点。")
                return HttpResponseForbidden("账号已停用，无法访问该端点。")
            if getattr(user, "is_superuser", False) and User.Role.ADMIN in allowed:
                return view(request, *args, **kwargs)
            if user.role not in allowed:
                _audit_denial(request, display, f"仅{display}可访问该端点，当前角色为 {user.get_role_display()}。")
                return HttpResponseForbidden(f"仅{display}可访问该端点。")
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def require_college_admin(view):
    """学院管理员专用端点（活动配置、白名单、终审）。"""
    return role_required(User.Role.ADMIN, label="学院管理员")(view)


def require_reviewer_or_admin(view):
    """审核老师或学院管理员专用端点（审核队列与审核动作）。"""
    return role_required(User.Role.REVIEWER, User.Role.ADMIN, label="审核老师或学院管理员")(view)
