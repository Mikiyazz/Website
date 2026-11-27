from functools import wraps

from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import redirect

from apps.common.errors import Forbidden

from .models import GRADE_RE, User


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


def format_class_scope_label(grade: str = "", major: str = "", class_name: str = "") -> str:
    """将 (grade, major, class_name) 格式化为统一班级展示文本（空项自动省略，兼容无年级/专业旧数据）。"""
    parts = [
        part
        for part in (
            str(grade or "").strip(),
            str(major or "").strip(),
            str(class_name or "").strip(),
        )
        if part
    ]
    return " ".join(parts)


def format_user_class_label(user) -> str:
    """格式化用户的完整班级展示文本（年级 + 专业 + 班级，缺省自动省略）。"""
    if user is None:
        return ""
    return format_class_scope_label(
        getattr(user, "grade", ""),
        getattr(user, "major", ""),
        getattr(user, "class_name", ""),
    )


def parse_class_filter(raw_value: str | None = None, *, grade: str | None = None, major: str | None = None) -> dict[str, str]:
    """解析班级筛选参数为 {"grade", "major", "class_name"}。

    兼容：
    - 完整展示标签："2025级 信息安全 01班" / "2025级 01班" / "信息安全 01班"
    - 管道分隔格式："2025级|信息安全|01班"
    - 显式分离参数：grade="2025级", major="信息安全", raw_value="01班"
    - 旧版单字段："信安25-1班" 或 "01班"
    """
    explicit_grade = str(grade or "").strip()
    explicit_major = str(major or "").strip()
    raw = str(raw_value or "").strip()

    if not raw and not explicit_grade and not explicit_major:
        return {"grade": "", "major": "", "class_name": ""}

    parsed_grade = explicit_grade
    parsed_major = explicit_major
    parsed_class = raw

    if raw and not explicit_grade and not explicit_major:
        if "|" in raw:
            parts = [p.strip() for p in raw.split("|")]
            if len(parts) >= 3:
                parsed_grade, parsed_major, parsed_class = parts[0], parts[1], parts[2]
            elif len(parts) == 2:
                parsed_grade, parsed_class = parts[0], parts[1]
        else:
            tokens = raw.split()
            if len(tokens) >= 3:
                parsed_grade = tokens[0]
                parsed_class = tokens[-1]
                parsed_major = " ".join(tokens[1:-1])
            elif len(tokens) == 2:
                first, second = tokens[0], tokens[1]
                if GRADE_RE.match(first) or first.endswith("级") or first.startswith("大"):
                    parsed_grade = first
                    parsed_class = second
                else:
                    parsed_major = first
                    parsed_class = second

    return {
        "grade": parsed_grade,
        "major": parsed_major,
        "class_name": parsed_class,
    }


def build_class_scope_q(
    raw_value: str | None = None,
    *,
    prefix: str = "",
    grade: str | None = None,
    major: str | None = None,
) -> Q:
    """根据 (grade, major, class_name) 构造精确 ORM 过滤 Q 条件（未填年级/专业时自动兼容旧数据）。"""
    parsed = parse_class_filter(raw_value, grade=grade, major=major)
    clause = Q()
    if parsed["class_name"]:
        clause &= Q(**{f"{prefix}class_name": parsed["class_name"]})
    if parsed["grade"]:
        clause &= Q(**{f"{prefix}grade": parsed["grade"]})
    if parsed["major"]:
        clause &= Q(**{f"{prefix}major": parsed["major"]})
    return clause


def scope_matches_class_filter(scope: dict[str, str], parsed_filter: dict[str, str]) -> bool:
    """判断单个审核员管辖 scope 是否覆盖目标班级筛选条件（防跨年级/跨专业同名班级越权）。"""
    scope_cls = str(scope.get("class_name") or "").strip()
    target_cls = str(parsed_filter.get("class_name") or "").strip()
    if not scope_cls or not target_cls or scope_cls != target_cls:
        return False

    scope_grade = str(scope.get("grade") or "").strip()
    target_grade = str(parsed_filter.get("grade") or "").strip()
    if scope_grade and target_grade and scope_grade != target_grade:
        return False

    scope_major = str(scope.get("major") or "").strip()
    target_major = str(parsed_filter.get("major") or "").strip()
    if scope_major and target_major and scope_major != target_major:
        return False

    return True


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


def reviewer_assigned_scopes(user, *, activity_id=None) -> list[dict[str, str]]:
    """读取团支书通过 ReviewAssignment 显式分配的精确 (grade, major, class_name) 范围列表。"""
    if not is_reviewer(user):
        return []
    from apps.reviews.models import ReviewAssignment

    qs = ReviewAssignment.objects.filter(reviewer=user)
    if activity_id is not None:
        qs = qs.filter(activity_id=activity_id)
    seen: set[tuple[str, str, str]] = set()
    scopes: list[dict[str, str]] = []
    for g, m, c in qs.values_list("grade", "major", "class_name"):
        clean_c = str(c or "").strip()
        if not clean_c:
            continue
        clean_g = str(g or "").strip()
        clean_m = str(m or "").strip()
        key = (clean_g, clean_m, clean_c)
        if key not in seen:
            seen.add(key)
            scopes.append({"grade": clean_g, "major": clean_m, "class_name": clean_c})
    scopes.sort(key=lambda item: (item["grade"], item["major"], item["class_name"]))
    return scopes


def reviewer_assigned_classes(user, *, activity_id=None) -> list[str]:
    """当团支书账号本身未填写班级时，读取显式配置的非空 ReviewAssignment 班级名列表。"""
    classes = {scope["class_name"] for scope in reviewer_assigned_scopes(user, activity_id=activity_id)}
    return sorted(classes)


def reviewer_allowed_class_scopes(user, *, activity_id=None) -> list[dict[str, str]]:
    """返回团支书允许管理的精确班级 scope 列表（默认拒绝：未绑定班级返回空列表）。"""
    scope = reviewer_own_class_scope(user)
    if scope is not None:
        return [scope]
    return reviewer_assigned_scopes(user, activity_id=activity_id)


def reviewer_allowed_class_labels(user, *, activity_id=None) -> list[str]:
    """返回团支书允许管理的完整班级展示标签列表（如「2025级 信息安全 01班」）。"""
    labels = {
        format_class_scope_label(s["grade"], s["major"], s["class_name"])
        for s in reviewer_allowed_class_scopes(user, activity_id=activity_id)
    }
    return sorted(lbl for lbl in labels if lbl)


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

    scopes = reviewer_allowed_class_scopes(user, activity_id=activity_id)
    if not scopes:
        return Q(pk__in=[])

    clause = Q(pk__in=[])
    for scope in scopes:
        item_q = Q(**{f"{prefix}class_name": scope["class_name"]})
        if scope["grade"]:
            item_q &= Q(**{f"{prefix}grade": scope["grade"]})
        if scope["major"]:
            item_q &= Q(**{f"{prefix}major": scope["major"]})
        clause |= item_q
    return clause


def is_target_in_reviewer_scope(actor, target_user, *, activity_id=None) -> bool:
    """判断目标学生是否位于操作者（学院管理员或本班团支书）的管辖班级内。"""
    if is_college_admin(actor):
        return True
    if not is_reviewer(actor) or target_user is None:
        return False

    target_class = str(getattr(target_user, "class_name", "") or "").strip()
    if not target_class:
        return False
    target_grade = str(getattr(target_user, "grade", "") or "").strip()
    target_major = str(getattr(target_user, "major", "") or "").strip()

    scopes = reviewer_allowed_class_scopes(actor, activity_id=activity_id)
    for scope in scopes:
        if target_class != scope["class_name"]:
            continue
        if scope["grade"] and target_grade != scope["grade"]:
            continue
        if scope["major"] and target_major != scope["major"]:
            continue
        return True
    return False


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
