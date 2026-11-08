import json
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.accounts.permissions import require_student
from apps.activities.selectors import eligible_activities, is_activity_open_for_user
from apps.audit.decorators import audit_rejections
from apps.audit.services import record_rejection
from apps.common.api import endpoint, json_body
from apps.common.errors import DomainError
from apps.uploads.policies import STANDARD_MATERIAL_RULES, rule_max_file_bytes
from apps.uploads.services import ensure_standard_material_requirements
from .models import Submission
from .services import create_draft, save_draft, submit_for_review


def serialize(item):
    return {
        "id": str(item.id),
        "activity_id": str(item.activity_id),
        "status": item.status,
        "version": item.version,
        "draft_data": item.draft_data,
    }


@endpoint("GET", "POST")
def collection(request):
    require_student(request.user)
    if request.method == "GET":
        items = Submission.objects.filter(owner=request.user).order_by("-updated_at")[:100]
        return JsonResponse({"items": [serialize(item) for item in items], "limit": 100})
    data = json_body(request)
    try:
        activity_id = uuid.UUID(str(data.get("activity_id", "")))
    except ValueError:
        raise DomainError("activity_id 必须为 UUID。") from None
    item = create_draft(actor=request.user, activity_id=activity_id)
    return JsonResponse(serialize(item), status=200)


@endpoint("GET", "PATCH")
@audit_rejections("submission.detail.rejected")
def detail(request, submission_id):
    require_student(request.user)
    # 归属校验写在查询条件里：他人 submission_id 只会得到 404，不会泄露记录是否存在。
    if request.method == "GET":
        item = Submission.objects.get(pk=submission_id, owner=request.user)
    else:
        data = json_body(request)
        item = save_draft(
            actor=request.user,
            submission_id=submission_id,
            expected_version=data.get("expected_version"),
            data=data.get("draft_data"),
        )
    return JsonResponse(serialize(item))


@endpoint("POST")
@audit_rejections("submission.submit.rejected")
def submit(request, submission_id):
    require_student(request.user)
    data = {}
    if request.body:
        try:
            data = json_body(request)
        except DomainError:
            pass
    request_key = None
    if "request_key" in data and data["request_key"]:
        try:
            request_key = uuid.UUID(str(data["request_key"]))
        except ValueError:
            raise DomainError("request_key 必须为 UUID。") from None

    submission, revision = submit_for_review(actor=request.user, submission_id=submission_id, request_key=request_key)
    return JsonResponse(
        {
            "submission": serialize(submission),
            "revision_id": str(revision.id),
            "revision_number": revision.number,
            "status": submission.status,
        },
        status=200,
    )

# =====================================================================
# 学生端「社会实践申报」四步向导（模板骨架）
# =====================================================================

# 允许进入申报向导的角色：所有在读学生（含兼任初审团支书与终审人员）。
APPLY_ALLOWED_ROLES = frozenset({"student", "grad_student", "reviewer", "college_admin"})

# 向导里五张材料卡片的展示顺序；与落库的五个标准类别完全一致，不再有别名映射。
WIZARD_MATERIAL_CODES = (
    "application_form",
    "practice_report",
    "scene_photos",
    "video",
    "other_materials",
)

MB = 1024 * 1024


def _build_wizard_material(code, requirements):
    """Turn one material rule into the flat, JS-friendly payload used by the FilePond zones."""
    spec = STANDARD_MATERIAL_RULES.get(code)
    if not spec:
        return None
    requirement = requirements.get(code) if requirements else None
    extensions = list(spec["allowed_extensions"])
    max_file_bytes = int(spec["max_file_bytes"])
    # 按扩展名的特批上限（当前只有 other_materials 的 .zip = 2000MB）。
    extension_caps = {
        ext: rule_max_file_bytes(code, ext) for ext in (spec.get("extension_max_bytes") or {})
    }
    extension_limits = [
        {"ext": ext, "max_mb": round(cap / MB, 2)} for ext, cap in extension_caps.items()
    ]
    # 按扩展名的份数上限（当前只有 other_materials 的 .zip = 5 份）。
    extension_count_limits = [
        {"ext": ext, "max_count": int(cap)}
        for ext, cap in (spec.get("extension_max_count") or {}).items()
    ]
    widest_bytes = max([max_file_bytes, *extension_caps.values()])
    limit_parts = [f"单份 ≤ {max_file_bytes // MB}MB"]
    for ext, cap in extension_caps.items():
        limit_parts.append(f"{ext.lstrip('.').upper()} 打包 ≤ {cap // MB}MB")
    server_extensions = list(getattr(requirement, "allowed_extensions", []) or [])
    server_max_bytes = int(getattr(requirement, "max_file_bytes", 0) or 0)
    if spec["required"]:
        count_hint = (
            f"必交 {spec['max_count']} 份"
            if spec["min_count"] == spec["max_count"]
            else f"必交 {spec['min_count']}～{spec['max_count']} 份"
        )
    else:
        count_hint = f"选交（最多 {spec['max_count']} 份）"
    if extension_count_limits:
        extra = "、".join(
            f"{item['ext'].lstrip('.').upper()} 最多 {item['max_count']} 份" for item in extension_count_limits
        )
        # 把扩展名份数并进同一个括号，避免出现「（最多 10 份）（其中 ZIP 最多 5 份）」这种套娃。
        count_hint = (count_hint[:-1] + "，" + extra + "）") if count_hint.endswith("）") else (count_hint + "（" + extra + "）")
    return {
        "code": code,
        "title": spec["title"],
        "required": bool(spec["required"]),
        "min_count": int(spec["min_count"]),
        "max_count": int(spec["max_count"]),
        "allowed_extensions": extensions,
        "accept_attr": ",".join(extensions),
        "max_file_bytes": max_file_bytes,
        "max_file_mb": round(max_file_bytes / MB, 2),
        "extension_limits": extension_limits,
        "extension_count_limits": extension_count_limits,
        "max_file_mb_widest": round(widest_bytes / MB, 2),
        "limit_label": " · ".join(limit_parts),
        "count_hint": count_hint,
        "requirement_id": str(requirement.id) if requirement is not None else "",
        "requirement_code": code,
        "server_allowed_extensions": server_extensions,
        "server_max_file_mb": round(server_max_bytes / MB, 2) if server_max_bytes else 0,
        "is_image_zone": code == "scene_photos",
        "allow_image_preview": code == "scene_photos",
        "allow_video_preview": False,
    }


@login_required
@require_GET
def apply(request):
    """Render the four-step social-practice application wizard (skeleton; saving lands next session)."""
    user = request.user
    if str(getattr(user, "role", "")) not in APPLY_ALLOWED_ROLES and not getattr(user, "is_superuser", False):
        # 申报向导只对学生开放：审核老师/管理员直接访问 URL 是越权尝试，落审计后重定向至工作台。
        record_rejection(
            action="access.role_denied",
            reason=f"申报向导仅对学生开放，当前角色为 {getattr(user, 'role', '') or 'anonymous'}。",
            user=user,
            target="",
            request=request,
            metadata={"required_role": "student", "view": "submission_apply"},
        )
        role_label = user.get_role_display() if hasattr(user, "get_role_display") else (getattr(user, "role", "") or "非学生")
        messages.warning(request, f"申报向导仅对学生开放，当前角色为{role_label}，已为您跳转至工作台。")
        return redirect("dashboard")

    open_activities = []
    for activity in eligible_activities(user).order_by("-created_at")[:50]:
        can_write_now, window_label = is_activity_open_for_user(activity, user)
        if can_write_now:
            open_activities.append({"activity": activity, "window_label": window_label})

    requested_id = (request.GET.get("activity_id") or "").strip()
    selected_activity = None
    window_label = ""
    for item in open_activities:
        if requested_id and str(item["activity"].id) == requested_id:
            selected_activity = item["activity"]
            window_label = item["window_label"]
            break
    if selected_activity is None and open_activities:
        selected_activity = open_activities[0]["activity"]
        window_label = open_activities[0]["window_label"]

    context = {
        "open_activities": [item["activity"] for item in open_activities],
        "selected_activity": selected_activity,
        "window_label": window_label,
        "submission": None,
        "materials": [],
        "materials_json": "[]",
        "submit_url": "",
        "detail_url": "",
        "success_url": "",
        "lookup_url": "/api/students/lookup/",
        "workbench_url": reverse("dashboard"),
        "draft_data": {"project_name": "", "declaration_type": "individual", "team_members": []},
        "team_members_json": "[]",
        "can_submit": False,
        "is_locked": False,
        "lock_reason": "",
    }

    if selected_activity is None:
        return render(request, "submissions/apply.html", context)

    requirements = ensure_standard_material_requirements(selected_activity)

    submission = Submission.objects.filter(owner=user, activity=selected_activity).first()
    if submission is None:
        try:
            submission = create_draft(actor=user, activity_id=selected_activity.id)
        except DomainError as exc:
            context["error_message"] = str(exc)

    materials = [
        material
        for material in (
            _build_wizard_material(code, requirements) for code in WIZARD_MATERIAL_CODES
        )
        if material
    ]

    raw_draft = submission.draft_data if submission is not None and isinstance(submission.draft_data, dict) else {}
    team_members = raw_draft.get("team_members") if isinstance(raw_draft.get("team_members"), list) else []
    declaration_type = str(raw_draft.get("declaration_type") or "").strip()
    if declaration_type not in ("individual", "team"):
        declaration_type = "team" if team_members else "individual"

    submittable = submission is not None and submission.status in (
        Submission.Status.DRAFT,
        Submission.Status.RETURNED,
    )
    context.update(
        {
            "submission": submission,
            "is_locked": submission is not None and not submittable,
            "lock_reason": (
                f"当前申报状态为【{submission.get_status_display()}】，表单与材料已只读锁定，如需修改请联系审核老师打回。"
                if submission is not None and not submittable
                else ""
            ),
            "materials": materials,
            "materials_json": json.dumps(materials, ensure_ascii=False),
            "draft_data": {
                "project_name": str(raw_draft.get("project_name") or raw_draft.get("title") or "").strip(),
                "declaration_type": declaration_type,
                "team_members": team_members,
            },
            "team_members_json": json.dumps(team_members, ensure_ascii=False),
            "submit_url": reverse("submission-submit", args=[submission.id]) if submission else "",
            "detail_url": reverse("submission-detail", args=[submission.id]) if submission else "",
            "success_url": (
                f"/?activity_id={selected_activity.id}&submission_id={submission.id}" if submission else ""
            ),
            "can_submit": submittable,
        }
    )
    return render(request, "submissions/apply.html", context)
