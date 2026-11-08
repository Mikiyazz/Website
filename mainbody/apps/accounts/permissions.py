from functools import wraps

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
