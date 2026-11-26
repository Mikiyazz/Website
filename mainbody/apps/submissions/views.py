import json
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.permissions import require_student
from apps.activities.models import Activity, DEFAULT_PROJECT_CATEGORIES
from apps.activities.selectors import eligible_activities, is_activity_open_for_user
from apps.audit.decorators import audit_rejections
from apps.audit.services import record_rejection
from apps.common.api import endpoint, json_body
from apps.common.errors import DomainError, Forbidden
from apps.uploads.policies import STANDARD_MATERIAL_RULES, rule_max_file_bytes
from apps.uploads.services import ensure_standard_material_requirements, map_uploads_by_submission
from .models import Submission
from .services import (
    can_delete_submission,
    create_draft,
    delete_submission,
    find_team_membership_activity_ids,
    find_team_membership_submissions,
    first_approved_submission_ids,
    is_empty_draft_submission,
    save_draft,
    submit_declaration,
    submit_for_review,
)


def serialize(item):
    return {
        "id": str(item.id),
        "activity_id": str(item.activity_id),
        "status": item.status,
        "version": item.version,
        "declaration_submitted": bool(item.declaration_submitted),
        "declaration_submitted_at": (
            item.declaration_submitted_at.isoformat() if item.declaration_submitted_at else ""
        ),
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
    force_new = bool(data.get("force_new") or data.get("new"))
    item = create_draft(actor=request.user, activity_id=activity_id, force_new=force_new)
    return JsonResponse(serialize(item), status=200)


@endpoint("GET", "PATCH", "DELETE")
@audit_rejections("submission.detail.rejected")
def detail(request, submission_id):
    require_student(request.user)
    # 归属校验写在查询条件里：他人 submission_id 只会得到 404，不会泄露记录是否存在。
    if request.method == "GET":
        item = Submission.objects.get(pk=submission_id, owner=request.user)
        return JsonResponse(serialize(item))
    if request.method == "DELETE":
        deleted = delete_submission(actor=request.user, submission_id=submission_id)
        return JsonResponse({"deleted": True, **deleted}, status=200)
    data = json_body(request)
    item = save_draft(
        actor=request.user,
        submission_id=submission_id,
        expected_version=data.get("expected_version"),
        data=data.get("draft_data"),
    )
    return JsonResponse(serialize(item))


@endpoint("POST")
@audit_rejections("submission.declare.rejected")
def declare(request, submission_id):
    """假期前立项申报提交（免审核，仅要求项目信息、团队成员与 .docx 申报表）。"""
    require_student(request.user)
    submission = submit_declaration(actor=request.user, submission_id=submission_id)
    return JsonResponse(
        {
            "submission": serialize(submission),
            "declaration_submitted": True,
            "declaration_submitted_at": (
                submission.declaration_submitted_at.isoformat() if submission.declaration_submitted_at else ""
            ),
            "status": submission.status,
        },
        status=200,
    )


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

# 允许进入申报/成果栏目的角色：所有在读学生（含兼任初审团支书与终审人员）。
APPLY_ALLOWED_ROLES = frozenset({"student", "grad_student", "reviewer", "college_admin"})

# 假期前「项目申报」仅包含申报表（.docx）；下学期「成果收集」包含其余 4 项成果材料。
DECLARATION_MATERIAL_CODES = ("application_form",)
OUTCOME_MATERIAL_CODES = (
    "practice_report",
    "scene_photos",
    "video",
    "other_materials",
)
WIZARD_MATERIAL_CODES = DECLARATION_MATERIAL_CODES + OUTCOME_MATERIAL_CODES

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


def _summarize_submission_card(
    sub,
    *,
    uploads,
    is_team_member: bool,
    is_selected: bool,
    can_write_now: bool = False,
    has_passed_first_review: bool = False,
):
    raw_draft = sub.draft_data if isinstance(sub.draft_data, dict) else {}
    team_members = raw_draft.get("team_members") if isinstance(raw_draft.get("team_members"), list) else []
    decl_type = str(raw_draft.get("declaration_type") or "").strip()
    if decl_type not in ("individual", "team"):
        decl_type = "team" if team_members else "individual"
    project_name = str(raw_draft.get("project_name") or raw_draft.get("title") or "").strip()
    category = str(raw_draft.get("category") or "").strip()

    submitted_at = sub.declaration_submitted_at
    app_uploads = [up for up in uploads if up.requirement.code == "application_form"]
    declaration_confirmed = bool(sub.declaration_submitted) and any(
        submitted_at is None or up.created_at <= submitted_at for up in app_uploads
    )
    outcomes_submitted = sub.status in (
        Submission.Status.PENDING_FIRST_REVIEW,
        Submission.Status.PENDING_FINAL_REVIEW,
        Submission.Status.FINAL_APPROVED,
    )
    checklist_done = int(declaration_confirmed) + int(outcomes_submitted)
    progress_percent = round((checklist_done / 2) * 100)
    can_delete = (
        not is_team_member
        and can_write_now
        and can_delete_submission(sub, has_passed_first_review=has_passed_first_review)
    )
    leader = sub.owner
    leader_label = f"{leader.real_name or leader.username}（{leader.username}）"
    if decl_type == "team":
        if is_team_member:
            team_summary = f"团队 · 队长 {leader.real_name or leader.username}"
        else:
            team_summary = f"团队 · {len(team_members)} 名队员"
    else:
        team_summary = "个人"

    return {
        "id": str(sub.id),
        "submission": sub,
        "project_name": project_name or "未命名草稿",
        "has_project_name": bool(project_name),
        "category": category or "—",
        "declaration_type": decl_type,
        "team_members_count": len(team_members),
        "team_summary": team_summary,
        "is_team_member": is_team_member,
        "team_leader_label": leader_label if is_team_member else "",
        "declaration_confirmed": declaration_confirmed,
        "outcomes_submitted": outcomes_submitted,
        "checklist_done": checklist_done,
        "progress_percent": progress_percent,
        "is_returned": sub.status == Submission.Status.RETURNED,
        "can_delete": can_delete,
        "delete_url": reverse("submission-delete-page", args=[sub.id]),
        "status_code": sub.status,
        "status_display": sub.get_status_display(),
        "badge_code": sub.badge_status,
        "badge_label": sub.badge_label,
        "upload_count": len(uploads),
        "updated_at": sub.updated_at,
        "is_selected": is_selected,
        "apply_url": f"{reverse('submission-apply-page')}?activity_id={sub.activity_id}&submission_id={sub.id}",
        "outcomes_url": f"{reverse('submission-outcomes-page')}?activity_id={sub.activity_id}&submission_id={sub.id}",
        "dashboard_url": f"{reverse('dashboard')}?activity_id={sub.activity_id}&submission_id={sub.id}",
    }


def _build_student_submission_page_context(request, *, material_codes, view_name: str):
    """构建学生端「我的申报」与「成果收集」共享页面上下文，支持多申报切换与覆盖修改。"""
    user = request.user
    if str(getattr(user, "role", "")) not in APPLY_ALLOWED_ROLES and not getattr(user, "is_superuser", False):
        record_rejection(
            action="access.role_denied",
            reason=f"申报与成果栏目仅对学生开放，当前角色为 {getattr(user, 'role', '') or 'anonymous'}。",
            user=user,
            target="",
            request=request,
            metadata={"required_role": "student", "view": view_name},
        )
        role_label = user.get_role_display() if hasattr(user, "get_role_display") else (getattr(user, "role", "") or "非学生")
        messages.warning(request, f"申报与成果栏目仅对学生开放，当前角色为{role_label}，已为您跳转至工作台。")
        return None, redirect("dashboard")

    eligible_qs = eligible_activities(user)
    submitted_act_ids = set(
        Submission.objects.filter(owner=user).values_list("activity_id", flat=True)
    ) | find_team_membership_activity_ids(user.username)
    all_acts = list(Activity.objects.all().order_by("-created_at")[:100])
    eligible_id_set = set(eligible_qs.values_list("id", flat=True)) | submitted_act_ids
    for act in all_acts:
        if act.is_in_late_whitelist(user.username):
            eligible_id_set.add(act.id)

    open_activities = []
    for activity in [act for act in all_acts if act.id in eligible_id_set][:50]:
        can_write_now, window_label = is_activity_open_for_user(activity, user)
        if can_write_now or activity.id in submitted_act_ids:
            open_activities.append(
                {
                    "activity": activity,
                    "can_write_now": can_write_now,
                    "window_label": window_label,
                }
            )

    requested_sub_id = (request.GET.get("submission_id") or "").strip()
    requested_id = (request.GET.get("activity_id") or "").strip()
    if not requested_id and requested_sub_id:
        try:
            hinted_sub = Submission.objects.filter(pk=uuid.UUID(requested_sub_id)).only("activity_id").first()
            if hinted_sub is not None:
                requested_id = str(hinted_sub.activity_id)
        except ValueError:
            pass

    selected_entry = None
    for item in open_activities:
        if requested_id and str(item["activity"].id) == requested_id:
            selected_entry = item
            break
    if selected_entry is None and open_activities:
        selected_entry = open_activities[0]

    selected_activity = selected_entry["activity"] if selected_entry else None
    selected_can_write_now = selected_entry["can_write_now"] if selected_entry else False
    window_label = selected_entry["window_label"] if selected_entry else ""

    context = {
        "open_activities": [item["activity"] for item in open_activities],
        "selected_activity": selected_activity,
        "selected_can_write_now": selected_can_write_now,
        "window_label": window_label,
        "submission": None,
        "own_submission_items": [],
        "member_submission_items": [],
        "all_submission_items": [],
        "materials": [],
        "materials_json": "[]",
        "declare_url": "",
        "submit_url": "",
        "detail_url": "",
        "success_url": "",
        "create_new_url": reverse("submission-create-new"),
        "apply_url": reverse("submission-apply-page"),
        "outcomes_url": reverse("submission-outcomes-page"),
        "lookup_url": "/api/students/lookup/",
        "workbench_url": reverse("dashboard"),
        "draft_data": {
            "project_name": "",
            "category": DEFAULT_PROJECT_CATEGORIES[0],
            "advisor_name": "",
            "practice_place": "",
            "date_start": "",
            "date_end": "",
            "project_intro": "",
            "declaration_type": "individual",
            "team_members": [],
        },
        "project_categories": list(DEFAULT_PROJECT_CATEGORIES),
        "legacy_category": "",
        "team_members_json": "[]",
        "declaration_submitted": False,
        "declaration_submitted_at": None,
        "declaration_form_uploads": [],
        "declaration_confirmed": False,
        "outcomes_submitted": False,
        "is_team_member_view": False,
        "team_leader": None,
        "can_submit": False,
        "is_locked": False,
        "lock_reason": "",
        "is_returned": False,
        "latest_returned_record": None,
        "review_records": [],
        "status_badge_code": Submission.Status.DRAFT,
        "status_badge_label": "草稿",
        "in_whitelist": False,
    }

    if selected_activity is None:
        return context, None

    requirements = ensure_standard_material_requirements(selected_activity)

    own_submissions = list(
        Submission.objects.select_related("owner")
        .filter(owner=user, activity=selected_activity)
        .order_by("-updated_at", "-created_at")
    )
    team_submissions = [
        sub
        for sub in find_team_membership_submissions(activity=selected_activity, student_number=user.username)
        if sub.owner_id != user.id
    ]

    submission = None
    is_team_member_view = False
    team_leader = None

    if requested_sub_id:
        for item in own_submissions:
            if str(item.id) == requested_sub_id:
                submission = item
                break
        if submission is None:
            for item in team_submissions:
                if str(item.id) == requested_sub_id:
                    submission = item
                    is_team_member_view = True
                    team_leader = item.owner
                    break

    skip_autocreate = (request.GET.get("deleted") or "").strip() == "1"
    if submission is None:
        non_empty_own = [item for item in own_submissions if not is_empty_draft_submission(item)]
        if non_empty_own:
            submission = non_empty_own[0]
        elif team_submissions:
            submission = team_submissions[0]
            is_team_member_view = True
            team_leader = submission.owner
        elif own_submissions:
            submission = own_submissions[0]
        elif selected_can_write_now and not skip_autocreate:
            try:
                submission = create_draft(actor=user, activity_id=selected_activity.id)
                own_submissions.insert(0, submission)
            except (DomainError, Forbidden) as exc:
                context["error_message"] = str(exc)

    all_subs_for_activity = own_submissions + team_submissions
    uploads_map = map_uploads_by_submission(
        all_subs_for_activity,
        activity=selected_activity,
    )
    first_approved_ids = first_approved_submission_ids([item.id for item in own_submissions])
    own_submission_items = [
        _summarize_submission_card(
            item,
            uploads=uploads_map.get(item.id, []),
            is_team_member=False,
            is_selected=bool(submission and item.id == submission.id),
            can_write_now=selected_can_write_now,
            has_passed_first_review=item.id in first_approved_ids,
        )
        for item in own_submissions
    ]
    member_submission_items = [
        _summarize_submission_card(
            item,
            uploads=uploads_map.get(item.id, []),
            is_team_member=True,
            is_selected=bool(submission and item.id == submission.id),
            can_write_now=False,
            has_passed_first_review=True,
        )
        for item in team_submissions
    ]
    all_submission_items = own_submission_items + member_submission_items

    active_uploads = uploads_map.get(submission.id, []) if submission is not None else []
    uploads_by_req = {}
    for up in active_uploads:
        ext = "." + up.original_name.rsplit(".", 1)[-1].lower() if "." in up.original_name else ""
        can_inline = ext in (".pdf", ".jpg", ".jpeg", ".png")
        asset = getattr(up, "fileasset", None)
        actual_bytes = asset.actual_bytes if asset else up.declared_bytes
        if actual_bytes >= MB:
            size_display = f"{round(actual_bytes / MB, 2)} MB"
        else:
            size_display = f"{round(actual_bytes / 1024, 1)} KB"
        uploads_by_req.setdefault(up.requirement.code, []).append(
            {
                "id": str(up.id),
                "original_name": up.original_name,
                "actual_bytes": actual_bytes,
                "size_display": size_display,
                "ext": ext,
                "can_inline_preview": can_inline,
                "preview_url": f"/api/v1/uploads/{up.id}/file/?inline=1" if can_inline else "",
                "download_url": f"/api/v1/uploads/{up.id}/file/",
            }
        )

    materials = [
        material
        for material in (
            _build_wizard_material(code, requirements) for code in material_codes
        )
        if material
    ]
    for m in materials:
        m["uploads"] = uploads_by_req.get(m["code"], [])

    raw_draft = submission.draft_data if submission is not None and isinstance(submission.draft_data, dict) else {}
    team_members = raw_draft.get("team_members") if isinstance(raw_draft.get("team_members"), list) else []
    declaration_type = str(raw_draft.get("declaration_type") or "").strip()
    if declaration_type not in ("individual", "team"):
        declaration_type = "team" if team_members else "individual"

    review_records = (
        list(submission.review_records.select_related("reviewer").order_by("-created_at"))
        if submission is not None
        else []
    )
    is_returned = submission is not None and submission.status == Submission.Status.RETURNED
    latest_returned_record = (
        submission.review_records.filter(decision="returned")
        .select_related("reviewer")
        .order_by("-created_at")
        .first()
        if is_returned
        else None
    )

    from apps.submissions.services import is_submission_locked_for_student

    status_locked = is_submission_locked_for_student(submission)
    submittable = (
        not is_team_member_view
        and selected_can_write_now
        and submission is not None
        and not status_locked
    )
    is_locked = not submittable
    if is_team_member_view and team_leader is not None:
        leader_label = f"{team_leader.real_name or team_leader.username}（{team_leader.username}）"
        lock_reason = f"当前查看的是由队长 {leader_label} 发起的团队申报，您作为团队成员可查看进度与材料。"
    elif status_locked and submission is not None:
        lock_reason = f"当前材料状态为「{submission.get_status_display()}」，已进入只读锁定状态；如需修改，请联系审核人员打回。"
    elif not selected_can_write_now:
        lock_reason = f"当前活动{window_label}，表单与材料已只读锁定。"
    else:
        lock_reason = ""

    project_categories = selected_activity.get_project_categories()
    saved_category = str(raw_draft.get("category") or "").strip()
    if saved_category and saved_category not in project_categories:
        legacy_category = saved_category
        current_category = saved_category
    else:
        legacy_category = ""
        current_category = saved_category or project_categories[0]

    declaration_submitted = bool(submission and submission.declaration_submitted)
    submitted_at = submission.declaration_submitted_at if submission else None
    if declaration_submitted:
        confirmed_app_ids = {
            str(up.id)
            for up in active_uploads
            if up.requirement.code == "application_form"
            and (submitted_at is None or up.created_at <= submitted_at)
        }
        declaration_form_uploads = [
            item
            for item in uploads_by_req.get("application_form", [])
            if item["id"] in confirmed_app_ids
        ]
    else:
        declaration_form_uploads = []
    declaration_confirmed = bool(declaration_submitted and declaration_form_uploads)
    outcomes_submitted = bool(
        submission
        and submission.status
        in (
            Submission.Status.PENDING_FIRST_REVIEW,
            Submission.Status.PENDING_FINAL_REVIEW,
            Submission.Status.FINAL_APPROVED,
        )
    )

    sub_query = f"&submission_id={submission.id}" if submission else ""
    context.update(
        {
            "submission": submission,
            "own_submission_items": own_submission_items,
            "member_submission_items": member_submission_items,
            "all_submission_items": all_submission_items,
            "is_locked": is_locked,
            "lock_reason": lock_reason,
            "is_returned": is_returned,
            "latest_returned_record": latest_returned_record,
            "review_records": review_records,
            "status_badge_code": submission.badge_status if submission else Submission.Status.DRAFT,
            "status_badge_label": submission.badge_label if submission else "草稿",
            "in_whitelist": selected_activity.is_in_late_whitelist(user.username),
            "materials": materials,
            "materials_json": json.dumps(materials, ensure_ascii=False),
            "project_categories": project_categories,
            "legacy_category": legacy_category,
            "declaration_submitted": declaration_submitted,
            "declaration_submitted_at": submission.declaration_submitted_at if submission else None,
            "declaration_form_uploads": declaration_form_uploads,
            "declaration_confirmed": declaration_confirmed,
            "outcomes_submitted": outcomes_submitted,
            "is_team_member_view": is_team_member_view,
            "team_leader": team_leader,
            "draft_data": {
                "project_name": str(raw_draft.get("project_name") or raw_draft.get("title") or "").strip(),
                "category": current_category,
                "advisor_name": str(raw_draft.get("advisor_name") or "").strip(),
                "practice_place": str(raw_draft.get("practice_place") or "").strip(),
                "date_start": str(raw_draft.get("date_start") or "").strip(),
                "date_end": str(raw_draft.get("date_end") or "").strip(),
                "project_intro": str(raw_draft.get("project_intro") or "").strip(),
                "declaration_type": declaration_type,
                "team_members": team_members,
            },
            "team_members_json": json.dumps(team_members, ensure_ascii=False),
            "declare_url": reverse("submission-declare", args=[submission.id]) if (submission and not is_team_member_view) else "",
            "submit_url": reverse("submission-submit", args=[submission.id]) if (submission and not is_team_member_view) else "",
            "detail_url": reverse("submission-detail", args=[submission.id]) if (submission and not is_team_member_view) else "",
            "apply_url": f"{reverse('submission-apply-page')}?activity_id={selected_activity.id}{sub_query}",
            "outcomes_url": f"{reverse('submission-outcomes-page')}?activity_id={selected_activity.id}{sub_query}",
            "workbench_url": f"{reverse('dashboard')}?activity_id={selected_activity.id}{sub_query}",
            "success_url": (
                f"/?activity_id={selected_activity.id}&submission_id={submission.id}" if submission else ""
            ),
            "can_submit": submittable,
        }
    )
    return context, None


@login_required
@require_POST
def create_new_submission(request):
    """在指定实践活动下新建一份独立的申报草稿并跳转编辑。"""
    user = request.user
    if str(getattr(user, "role", "")) not in APPLY_ALLOWED_ROLES and not getattr(user, "is_superuser", False):
        messages.warning(request, "当前账号角色无权创建社会实践申报。")
        return redirect("dashboard")

    raw_activity_id = (request.POST.get("activity_id") or "").strip()
    if not raw_activity_id:
        messages.error(request, "请先选择要申报的社会实践活动。")
        return redirect("submission-apply-page")
    try:
        activity_uuid = uuid.UUID(raw_activity_id)
    except ValueError:
        messages.error(request, "实践活动标识无效。")
        return redirect("submission-apply-page")

    try:
        submission = create_draft(actor=user, activity_id=activity_uuid, force_new=True)
    except (DomainError, Forbidden, Activity.DoesNotExist) as exc:
        messages.error(request, str(exc))
        return redirect(f"{reverse('submission-apply-page')}?activity_id={raw_activity_id}")

    messages.success(request, "已新建申报草稿，请填写项目信息与团队成员。")
    return redirect(f"{reverse('submission-apply-page')}?activity_id={submission.activity_id}&submission_id={submission.id}")


@login_required
@require_POST
def delete_submission_view(request, submission_id):
    """删除本人名下尚未通过初审的申报记录及关联附件。"""
    user = request.user
    if str(getattr(user, "role", "")) not in APPLY_ALLOWED_ROLES and not getattr(user, "is_superuser", False):
        messages.warning(request, "当前账号角色无权删除社会实践申报。")
        return redirect("dashboard")

    raw_next = (request.POST.get("next") or "").strip()
    allowed_targets = {
        "dashboard": reverse("dashboard"),
        "apply": reverse("submission-apply-page"),
        "outcomes": reverse("submission-outcomes-page"),
    }
    target_path = allowed_targets.get(raw_next, reverse("submission-apply-page"))
    fallback_activity_id = (request.POST.get("activity_id") or "").strip()

    try:
        deleted = delete_submission(actor=user, submission_id=submission_id)
    except (DomainError, Forbidden, Submission.DoesNotExist) as exc:
        messages.error(request, str(exc) if str(exc) else "申报记录不存在或无法删除。")
        suffix = f"?activity_id={fallback_activity_id}" if fallback_activity_id else ""
        return redirect(f"{target_path}{suffix}")

    messages.success(request, "申报记录已删除。")
    act_id = deleted.get("activity_id") or fallback_activity_id
    suffix = f"?activity_id={act_id}&deleted=1" if act_id else "?deleted=1"
    return redirect(f"{target_path}{suffix}")


@login_required
@require_GET
def apply(request):
    """假期前「项目申报」栏目：项目信息、团队成员与 .docx 申报表（免审核）。"""
    context, redirect_resp = _build_student_submission_page_context(
        request,
        material_codes=DECLARATION_MATERIAL_CODES,
        view_name="submission_apply",
    )
    if redirect_resp is not None:
        return redirect_resp
    return render(request, "submissions/apply.html", context)


@login_required
@require_GET
def outcomes(request):
    """下学期开学后「成果收集」栏目：实践报告、现场照片、宣讲视频与其他证明材料（提交后进入审核）。"""
    context, redirect_resp = _build_student_submission_page_context(
        request,
        material_codes=OUTCOME_MATERIAL_CODES,
        view_name="submission_outcomes",
    )
    if redirect_resp is not None:
        return redirect_resp
    return render(request, "submissions/outcomes.html", context)
