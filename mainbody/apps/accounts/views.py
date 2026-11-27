"""Accounts and workbench views: registration, password change, and role-based web workbenches."""
import json
import re
import uuid
from django.contrib import messages
from django.contrib.auth import login as auth_login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from apps.accounts.models import User
from apps.accounts.permissions import (
    build_class_scope_q,
    format_class_scope_label,
    format_user_class_label,
    is_college_admin,
    is_reviewer,
    is_target_in_reviewer_scope,
    parse_class_filter,
    require_college_admin,
    require_reviewer_or_admin,
    require_student,
    reviewer_allowed_class_labels,
    reviewer_allowed_class_names,
    reviewer_user_scope_q,
    scope_matches_class_filter,
)
from apps.accounts.models import StudentRoster
from apps.activities.models import Activity, DEFAULT_PROJECT_CATEGORIES, parse_project_categories
from apps.activities.selectors import eligible_activities, is_activity_open_for_user
from apps.activities.services import (
    parse_student_numbers,
    resolve_known_student_numbers,
    update_late_submission_settings,
)
from apps.audit.services import record
from apps.common.api import endpoint, json_body
from apps.common.errors import DomainError, Forbidden
from apps.exports.services import list_activity_declared_classes, resolve_status_filter
from apps.reviews.models import ReviewRecord
from apps.reviews.selectors import (
    WORKBENCH_MAX_PAGE_SIZE,
    WORKBENCH_PAGE_SIZE,
    collect_workbench_page,
    collect_workbench_rows,
    declaration_type_filter_q,
    normalize_declaration_type_filter,
    parse_page_size,
    reviewable_submissions,
    summarize_submission_counts,
    workbench_activities,
    workbench_class_options,
    workbench_whitelist_numbers,
)
from apps.reviews.services import review_submission
from apps.reviews.views import serialize_submission_for_review
from apps.submissions.models import Submission, SubmissionTeamMember
from apps.submissions.services import (
    _team_submission_priority,
    can_delete_submission,
    create_draft,
    find_all_team_membership_submissions,
    find_team_membership_activity_ids,
    find_team_membership_submissions,
    first_approved_submission_ids,
    is_empty_draft_submission,
    map_activity_team_memberships_all,
    save_draft,
    submit_for_review,
)
from apps.submissions.views import _summarize_submission_card
from apps.uploads.models import FileAsset, UploadSession
from apps.uploads.policies import INLINE_PREVIEW_EXTENSIONS, STANDARD_MATERIAL_RULES
from apps.uploads.services import ensure_standard_material_requirements, map_uploads_by_submission
from .services import change_password, register_student

DASHBOARD_SUBMISSION_PAGE_SIZE = 25
ACCOUNT_PROGRESS_PAGE_SIZE = 25


def _parse_uuid_or_domain_error(raw_value, field_label: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw_value).strip())
    except (ValueError, TypeError, AttributeError):
        raise DomainError(f"{field_label} 必须为有效 UUID。") from None


def _query_without_param(request, param_name: str) -> str:
    params = request.GET.copy()
    params.pop(param_name, None)
    return params.urlencode()


def _elided_page_range(page_obj) -> list:
    if page_obj is None or page_obj.paginator.num_pages <= 1:
        return []
    return list(
        page_obj.paginator.get_elided_page_range(
            page_obj.number,
            on_each_side=1,
            on_ends=1,
        )
    )


def serialize_user(user):
    return {
        "id": str(user.id),
        "student_number": user.username,
        "real_name": user.real_name,
        "class_name": user.class_name,
        "grade": user.grade,
        "major": user.major,
        "phone": user.phone,
        "role": user.role,
        "must_change_password": user.must_change_password,
    }


@endpoint("POST", require_auth=False)
def register(request):
    data = json_body(request)
    student_number = data.get("student_number") or data.get("username")
    real_name = data.get("real_name")
    class_name = data.get("class_name")
    password = data.get("password")
    grade = data.get("grade", "")
    major = data.get("major", "")
    phone = data.get("phone", "")

    user = register_student(
        student_number=student_number,
        real_name=real_name,
        class_name=class_name,
        password=password,
        grade=grade,
        major=major,
        phone=phone,
    )
    return JsonResponse({"user": serialize_user(user)}, status=201)


@endpoint("POST", require_auth=True)
def change_password_view(request):
    data = json_body(request)
    old_password = data.get("old_password")
    if old_password is None:
        old_password = data.get("current_password")
    new_password = data.get("new_password") or data.get("password")

    user = change_password(
        user=request.user,
        old_password=old_password,
        new_password=new_password,
    )
    update_session_auth_hash(request, user)
    return JsonResponse({"user": serialize_user(user)}, status=200)


# =====================================================================
# Student lookup: used by the team-member rows of the apply wizard
# =====================================================================

@endpoint("GET")
def student_lookup_view(request):
    """Placeholder lookup endpoint: {"exists": true, "real_name": "张三"} or {"exists": false}.

    Reads the account table first and falls back to the imported StudentRoster.
    """
    require_student(request.user)
    student_number = (request.GET.get("student_number") or "").strip()
    if not student_number:
        raise DomainError("请提供 student_number 查询参数。")

    account = User.objects.filter(username=student_number, is_active=True).first()
    if account is not None and (account.role in (User.Role.STUDENT, User.Role.REVIEWER, User.Role.ADMIN) or account.is_active):
        return JsonResponse(
            {
                "exists": True,
                "real_name": account.real_name or "",
                "student_number": account.username,
                "class_name": account.class_name or "",
                "grade": account.grade or "",
                "major": account.major or "",
                "source": "account",
            }
        )

    roster = StudentRoster.objects.filter(student_number=student_number, active=True).first()
    if roster is None:
        return JsonResponse({"exists": False, "student_number": student_number})

    return JsonResponse(
        {
            "exists": True,
            "real_name": roster.real_name or "",
            "student_number": roster.student_number,
            "class_name": roster.class_name or "",
            "grade": roster.grade or "",
            "major": roster.major or "",
            "source": "roster",
        }
    )


# =====================================================================
# HTML Page Views: Registration & Password Change
# =====================================================================

@require_http_methods(["GET", "POST"])
def register_page_view(request):
    from apps.accounts.models import MAJOR_CHOICES

    if request.method == "GET":
        return render(
            request,
            "registration/register.html",
            {
                "major_choices": MAJOR_CHOICES,
                "form_data": {},
                "field_errors": {},
            },
        )

    if request.content_type == "application/json":
        return register(request)

    form_data = {
        "student_number": request.POST.get("student_number", "").strip(),
        "real_name": request.POST.get("real_name", "").strip(),
        "grade": request.POST.get("grade", "").strip(),
        "major": request.POST.get("major", "").strip(),
        "class_name": request.POST.get("class_name", "").strip(),
        "phone": request.POST.get("phone", "").strip(),
        "password": request.POST.get("password", ""),
    }
    try:
        user = register_student(
            student_number=form_data["student_number"],
            real_name=form_data["real_name"],
            class_name=form_data["class_name"],
            password=form_data["password"] or None,
            grade=form_data["grade"],
            major=form_data["major"],
            phone=form_data["phone"],
        )
    except DomainError as exc:
        field_errors = getattr(exc, "field_errors", None) or {}
        return render(
            request,
            "registration/register.html",
            {
                "major_choices": MAJOR_CHOICES,
                "form_data": form_data,
                "field_errors": field_errors,
                "error_message": str(exc),
            },
            status=exc.status,
        )

    auth_login(request, user)
    messages.success(request, "账号注册成功！首次登录请尽快修改初始密码。")
    next_url = request.POST.get("next") or request.GET.get("next") or "/"
    return redirect(next_url)


@login_required
@require_http_methods(["GET", "POST"])
def change_password_page_view(request):
    if request.method == "GET":
        return render(request, "registration/change_password.html", {})

    if request.content_type == "application/json":
        return change_password_view(request)

    old_password = request.POST.get("old_password") or request.POST.get("current_password") or ""
    new_password = request.POST.get("new_password") or request.POST.get("password") or ""
    confirm_password = request.POST.get("confirm_password", "")

    try:
        if confirm_password and confirm_password != new_password:
            raise DomainError("两次输入的新密码不一致。")
        user = change_password(
            user=request.user,
            old_password=old_password,
            new_password=new_password,
        )
        update_session_auth_hash(request, user)
    except DomainError as exc:
        return render(
            request,
            "registration/change_password.html",
            {"error_message": str(exc)},
            status=exc.status,
        )

    messages.success(request, "密码修改成功！")
    next_url = request.POST.get("next") or request.GET.get("next") or "/"
    return redirect(next_url)


# =====================================================================
# Workbench Helpers & Views (Student / Reviewer / Admin)
# =====================================================================

def _format_bytes(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.2f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes} B"


def _build_student_workbench_context(request):
    user = request.user
    all_own_subs = list(
        Submission.objects.select_related("owner")
        .filter(owner=user)
        .order_by("-updated_at", "-created_at")
    )
    own_submissions_by_act: dict[uuid.UUID, list[Submission]] = {}
    for sub in all_own_subs:
        own_submissions_by_act.setdefault(sub.activity_id, []).append(sub)

    all_team_subs_by_act = find_all_team_membership_submissions(user.username)
    submitted_act_ids = set(own_submissions_by_act.keys()) | set(all_team_subs_by_act.keys())

    all_acts = list(Activity.objects.all().order_by("-created_at")[:100])
    user_is_admin = is_college_admin(user)
    user_class = str(getattr(user, "class_name", "") or "").strip()
    eligible_id_set = set(submitted_act_ids)
    for act in all_acts:
        if (
            not act.allowed_classes
            or (user_class and user_class in act.allowed_classes)
            or (user_is_admin and not user_class)
            or act.is_in_late_whitelist(user.username)
        ):
            eligible_id_set.add(act.id)

    activities = [act for act in all_acts if act.id in eligible_id_set]

    team_submissions_by_act: dict[uuid.UUID, list[Submission]] = {}
    submissions_by_act = {}
    team_member_act_ids = set()
    for act in activities:
        own_subs = own_submissions_by_act.get(act.id, [])
        team_subs = [
            sub
            for sub in all_team_subs_by_act.get(act.id, [])
            if sub.owner_id != user.id
        ]
        team_submissions_by_act[act.id] = team_subs
        if own_subs:
            submissions_by_act[act.id] = own_subs[0]
        elif team_subs:
            submissions_by_act[act.id] = team_subs[0]
            team_member_act_ids.add(act.id)

    requested_sub_id = (request.GET.get("submission_id") or "").strip()
    selected_act_id = (request.GET.get("activity_id") or "").strip()
    if not selected_act_id and requested_sub_id:
        try:
            hinted_sub = Submission.objects.filter(pk=uuid.UUID(requested_sub_id)).only("activity_id").first()
            if hinted_sub is not None:
                selected_act_id = str(hinted_sub.activity_id)
        except ValueError:
            pass

    selected_activity = None
    if selected_act_id:
        for act in activities:
            if str(act.id) == str(selected_act_id):
                selected_activity = act
                break
    if selected_activity is None and activities:
        selected_activity = activities[0]

    activity_cards = []
    for act in activities:
        sub = submissions_by_act.get(act.id)
        own_count = len(own_submissions_by_act.get(act.id, []))
        member_count = len(team_submissions_by_act.get(act.id, []))
        can_write_now, window_label = is_activity_open_for_user(act, user)
        in_whitelist = act.is_in_late_whitelist(user.username)
        status_code = sub.status if sub else Submission.Status.DRAFT
        status_display = sub.get_status_display() if sub else "草稿"
        badge_code = sub.badge_status if sub else Submission.Status.DRAFT
        badge_label = sub.badge_label if sub else "草稿"
        activity_cards.append(
            {
                "activity": act,
                "submission": sub,
                "submission_count": own_count + member_count,
                "own_submission_count": own_count,
                "status_code": status_code,
                "status_display": status_display,
                "badge_code": badge_code,
                "badge_label": badge_label,
                "can_write_now": can_write_now,
                "window_label": window_label,
                "in_whitelist": in_whitelist,
                "is_selected": bool(selected_activity and act.id == selected_activity.id),
            }
        )

    submission = None
    own_submission_items = []
    member_submission_items = []
    all_submission_items = []
    selected_can_write_now = False
    is_team_member_view = False
    team_leader = None
    is_locked = True
    lock_reason = ""
    is_returned = False
    latest_returned_record = None
    review_records = []
    default_category = (
        selected_activity.get_project_categories()[0]
        if selected_activity is not None
        else DEFAULT_PROJECT_CATEGORIES[0]
    )
    draft_data = {
        "project_name": "",
        "category": default_category,
        "advisor_name": "",
        "practice_place": "",
        "date_start": "",
        "date_end": "",
        "project_intro": "",
        "declaration_type": "individual",
        "team_members": [],
    }
    material_sections = []
    uploads_by_req = {}
    active_uploads = []

    first_approved_ids: set[uuid.UUID] = set()
    if selected_activity is not None:
        reqs_map = ensure_standard_material_requirements(selected_activity)
        can_write_now, window_label = is_activity_open_for_user(selected_activity, user)
        selected_can_write_now = can_write_now
        own_subs = list(own_submissions_by_act.get(selected_activity.id, []))
        team_subs = list(team_submissions_by_act.get(selected_activity.id, []))

        if requested_sub_id:
            for item in own_subs:
                if str(item.id) == requested_sub_id:
                    submission = item
                    break
            if submission is None:
                for item in team_subs:
                    if str(item.id) == requested_sub_id:
                        submission = item
                        is_team_member_view = True
                        team_leader = item.owner
                        break

        # 禁止学生首页 GET / 自动 create_draft()：仅展示已有申报；新草稿在进入填报页或首次保存/上传时按需创建。
        if submission is None:
            non_empty_own = [item for item in own_subs if not is_empty_draft_submission(item)]
            if non_empty_own:
                submission = non_empty_own[0]
            elif team_subs:
                submission = team_subs[0]
                is_team_member_view = True
                team_leader = submission.owner
            elif own_subs:
                submission = own_subs[0]

        uploads_map = map_uploads_by_submission(own_subs + team_subs, activity=selected_activity)
        first_approved_ids = first_approved_submission_ids([item.id for item in own_subs])
        own_submission_items = [
            _summarize_submission_card(
                item,
                uploads=uploads_map.get(item.id, []),
                is_team_member=False,
                is_selected=bool(submission and item.id == submission.id),
                can_write_now=can_write_now,
                has_passed_first_review=item.id in first_approved_ids,
            )
            for item in own_subs
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
            for item in team_subs
        ]
        all_submission_items = own_submission_items + member_submission_items

        if submission is not None:
            raw_draft = submission.draft_data if isinstance(submission.draft_data, dict) else {}
            team_members = raw_draft.get("team_members") if isinstance(raw_draft.get("team_members"), list) else []
            decl_type = str(raw_draft.get("declaration_type") or "").strip()
            if decl_type not in ("individual", "team"):
                decl_type = "team" if team_members else "individual"
            draft_data = {
                "project_name": str(raw_draft.get("project_name") or raw_draft.get("title") or "").strip(),
                "category": str(raw_draft.get("category") or default_category).strip() or default_category,
                "advisor_name": str(raw_draft.get("advisor_name") or "").strip(),
                "practice_place": str(raw_draft.get("practice_place") or "").strip(),
                "date_start": str(raw_draft.get("date_start") or "").strip(),
                "date_end": str(raw_draft.get("date_end") or "").strip(),
                "project_intro": str(raw_draft.get("project_intro") or "").strip(),
                "declaration_type": decl_type,
                "team_members": team_members,
            }
            review_records = list(
                submission.review_records.select_related("reviewer").order_by("-created_at")
            )
            if submission.status == Submission.Status.RETURNED:
                is_returned = True
                latest_returned_record = next(
                    (rec for rec in review_records if rec.decision == "returned"),
                    None,
                )
            from apps.submissions.services import is_submission_locked_for_student

            if is_team_member_view and team_leader is not None:
                is_locked = True
                leader_label = f"{team_leader.real_name or team_leader.username}（{team_leader.username}）"
                lock_reason = f"当前查看的是由队长 {leader_label} 发起的团队申报，您作为团队成员可查看进度与材料。"
            elif is_submission_locked_for_student(submission):
                is_locked = True
                lock_reason = f"当前材料状态为「{submission.get_status_display()}」，已进入只读锁定状态；如需修改，请联系审核人员打回。"
            elif not can_write_now:
                is_locked = True
                lock_reason = f"当前活动{window_label}，当前申报已只读锁定。"
            else:
                is_locked = False
        elif not can_write_now:
            is_locked = True
            lock_reason = f"当前活动{window_label}，暂不可创建或修改申报。"
        else:
            is_locked = False
            lock_reason = ""

        active_uploads = uploads_map.get(submission.id, []) if submission is not None else []
        for up in active_uploads:
            ext = "." + up.original_name.rsplit(".", 1)[-1].lower() if "." in up.original_name else ""
            can_inline = ext in INLINE_PREVIEW_EXTENSIONS
            asset = getattr(up, "fileasset", None)
            actual_bytes = asset.actual_bytes if asset else up.declared_bytes
            uploads_by_req.setdefault(up.requirement.code, []).append(
                {
                    "id": str(up.id),
                    "original_name": up.original_name,
                    "actual_bytes": actual_bytes,
                    "size_display": _format_bytes(actual_bytes),
                    "ext": ext,
                    "can_inline_preview": can_inline,
                    "preview_url": f"/api/v1/uploads/{up.id}/file/?inline=1" if can_inline else "",
                    "download_url": f"/api/v1/uploads/{up.id}/file/",
                }
            )

        ordered_codes = ["application_form", "practice_report", "scene_photos", "video", "other_materials"]
        for code in ordered_codes:
            req = reqs_map.get(code)
            if not req:
                continue
            spec = STANDARD_MATERIAL_RULES.get(code, {})
            max_mb = int(req.max_file_bytes // (1024 * 1024))
            exts = list(req.allowed_extensions or spec.get("allowed_extensions", []))
            if req.required:
                if req.min_count == req.max_count:
                    count_hint = f"必交 {req.max_count} 份"
                else:
                    count_hint = f"必交 {req.min_count}～{req.max_count} 份"
            else:
                count_hint = f"选交（最多 {req.max_count} 份）"

            material_sections.append(
                {
                    "requirement": req,
                    "code": code,
                    "stage": "declaration" if code == "application_form" else "outcomes",
                    "stage_label": "项目申报" if code == "application_form" else "成果收集",
                    "title": req.title,
                    "required": req.required,
                    "min_count": req.min_count,
                    "max_count": req.max_count,
                    "max_file_bytes": req.max_file_bytes,
                    "max_mb": max_mb,
                    "allowed_extensions": exts,
                    "allowed_exts_display": " / ".join(exts),
                    "accept_attr": ",".join(exts),
                    "allowed_exts_json": json.dumps(exts),
                    "count_hint": count_hint,
                    "is_single": req.max_count == 1,
                    "uploads": uploads_by_req.get(code, []),
                }
            )

    app_count = len(uploads_by_req.get("application_form", []))
    rep_count = len(uploads_by_req.get("practice_report", []))
    photo_count = len(uploads_by_req.get("scene_photos", []))
    vid_count = len(uploads_by_req.get("video", []))
    oth_count = len(uploads_by_req.get("other_materials", []))
    uploaded_categories = sum(1 for c in (app_count, rep_count, photo_count, vid_count, oth_count) if c > 0)
    has_basic = bool(draft_data["project_name"])
    is_team = draft_data["declaration_type"] == "team"
    team_count = len(draft_data["team_members"])
    team_ready = (not is_team) or team_count > 0
    declaration_submitted_flag = bool(submission and submission.declaration_submitted)
    submitted_at = submission.declaration_submitted_at if submission else None
    declaration_submitted = declaration_submitted_flag and any(
        up.requirement.code == "application_form"
        and (submitted_at is None or up.created_at <= submitted_at)
        for up in active_uploads
    )
    outcomes_submitted = bool(
        submission
        and submission.status
        in (
            Submission.Status.PENDING_FIRST_REVIEW,
            Submission.Status.PENDING_FINAL_REVIEW,
            Submission.Status.FINAL_APPROVED,
        )
    )
    checks = {
        "declaration": declaration_submitted,
        "outcomes": outcomes_submitted,
    }
    required_done = int(app_count >= 1) + int(rep_count >= 1) + int(photo_count >= 3)
    outcomes_required_done = int(rep_count >= 1) + int(photo_count >= 3)
    checklist_done = int(checks["declaration"]) + int(checks["outcomes"])
    student_overview = {
        "uploaded_categories": uploaded_categories,
        "has_basic": has_basic,
        "is_team": is_team,
        "team_count": team_count,
        "team_ready": team_ready,
        "declaration_submitted": declaration_submitted,
        "declaration_submitted_at": submission.declaration_submitted_at if submission else None,
        "outcomes_submitted": outcomes_submitted,
        "checks": checks,
        "required_done": required_done,
        "outcomes_required_done": outcomes_required_done,
        "checklist_done": checklist_done,
        "progress_percent": round((checklist_done / 2) * 100),
        "total_files": sum(len(sec["uploads"]) for sec in material_sections),
    }

    can_delete_current = bool(
        submission is not None
        and not is_team_member_view
        and selected_can_write_now
        and can_delete_submission(
            submission,
            has_passed_first_review=(submission.id in first_approved_ids),
        )
    )

    return {
        "workbench_role": "student",
        "activity_cards": activity_cards,
        "selected_activity": selected_activity,
        "selected_can_write_now": selected_can_write_now,
        "submission": submission,
        "can_delete_current": can_delete_current,
        "own_submission_items": own_submission_items,
        "member_submission_items": member_submission_items,
        "all_submission_items": all_submission_items,
        "status_code": submission.status if submission else Submission.Status.DRAFT,
        "status_display": submission.get_status_display() if submission else "草稿",
        # 徽章口径与审核工作台一致：命中补交白名单的迟交材料显示 LATE_SUBMITTED。
        "status_badge_code": submission.badge_status if submission else Submission.Status.DRAFT,
        "status_badge_label": submission.badge_label if submission else "草稿",
        "is_locked": is_locked,
        "lock_reason": lock_reason,
        "is_team_member_view": is_team_member_view,
        "team_leader": team_leader,
        "is_returned": is_returned,
        "latest_returned_record": latest_returned_record,
        "review_records": review_records,
        "draft_data": draft_data,
        "team_members_json": json.dumps(draft_data["team_members"], ensure_ascii=False),
        "material_sections": material_sections,
        "student_overview": student_overview,
    }


def _build_admin_account_progress(request, selected_activity, *, actor=None):
    """构建注册账号信息与提交情况板块数据：管理员分页看全院，团支书仅查询本班及其团队关联。"""
    viewer = actor or request.user
    viewer_is_admin = is_college_admin(viewer)
    selected_activity_id = selected_activity.id if selected_activity is not None else None

    acct_submit_filter = (request.GET.get("acct_submit") or "").strip()
    acct_class_filter = (request.GET.get("acct_class") or "").strip()
    acct_decl_type_filter = (request.GET.get("acct_decl_type") or "").strip()
    acct_q_filter = (request.GET.get("acct_q") or "").strip()
    acct_page_num = request.GET.get("acct_page") or 1
    acct_page_size = parse_page_size(
        request.GET.get("acct_page_size"),
        default=ACCOUNT_PROGRESS_PAGE_SIZE,
        maximum=WORKBENCH_MAX_PAGE_SIZE,
    )

    registered_qs = User.objects.filter(role=User.Role.STUDENT, is_active=True)
    if not viewer_is_admin:
        registered_qs = registered_qs.filter(
            reviewer_user_scope_q(viewer, prefix="", activity_id=selected_activity_id)
        )

    user_tuples = list(
        registered_qs.order_by("grade", "major", "class_name", "username").values_list(
            "id", "username", "real_name", "grade", "major", "class_name"
        )
    )
    acct_classes = sorted(
        {
            format_class_scope_label(g, m, c)
            for _, _, _, g, m, c in user_tuples
            if str(c or "").strip()
        }
    )

    status_rank_map = {
        Submission.Status.FINAL_APPROVED: 5,
        Submission.Status.PENDING_FINAL_REVIEW: 4,
        Submission.Status.PENDING_FIRST_REVIEW: 3,
        Submission.Status.RETURNED: 2,
        Submission.Status.DRAFT: 1,
    }
    outcome_done_statuses = frozenset(
        {
            Submission.Status.PENDING_FIRST_REVIEW,
            Submission.Status.PENDING_FINAL_REVIEW,
            Submission.Status.FINAL_APPROVED,
        }
    )

    own_sub_ids_by_uid: dict[int, list[uuid.UUID]] = {}
    team_sub_ids_by_sn: dict[str, list[uuid.UUID]] = {}
    sub_meta_by_id: dict[uuid.UUID, tuple] = {}
    decl_done_sub_ids: set[uuid.UUID] = set()

    if selected_activity is not None and user_tuples:
        scoped_user_ids = [row[0] for row in user_tuples]
        scoped_usernames = [row[1] for row in user_tuples]

        if viewer_is_admin:
            tm_qs = SubmissionTeamMember.objects.filter(activity_id=selected_activity.id)
        else:
            tm_qs = SubmissionTeamMember.objects.filter(
                activity_id=selected_activity.id,
                student_number__in=scoped_usernames,
            )

        team_sub_id_set: set[uuid.UUID] = set()
        for sn, sub_id in tm_qs.values_list("student_number", "submission_id"):
            clean_sn = str(sn or "").strip()
            if clean_sn and sub_id:
                team_sub_ids_by_sn.setdefault(clean_sn, []).append(sub_id)
                team_sub_id_set.add(sub_id)

        # 团支书严格限定查询本班学生本人申报与本班学生参与的团队申报，绝不拉取全院申报。
        sub_qs = Submission.objects.filter(activity=selected_activity)
        if not viewer_is_admin:
            sub_scope_q = Q(owner_id__in=scoped_user_ids)
            if team_sub_id_set:
                sub_scope_q |= Q(id__in=team_sub_id_set)
            sub_qs = sub_qs.filter(sub_scope_q)

        owner_sub_counts: dict[int, int] = {}
        single_sub_by_owner: dict[int, uuid.UUID] = {}
        for sid, owner_id, status_code, decl_sub, decl_at, updated_at, draft_data in sub_qs.values_list(
            "id",
            "owner_id",
            "status",
            "declaration_submitted",
            "declaration_submitted_at",
            "updated_at",
            "draft_data",
        ):
            owner_sub_counts[owner_id] = owner_sub_counts.get(owner_id, 0) + 1
            single_sub_by_owner[owner_id] = sid
            sub_meta_by_id[sid] = (owner_id, status_code, bool(decl_sub), decl_at, updated_at, draft_data)
            is_empty = (
                status_code == Submission.Status.DRAFT
                and not decl_sub
                and (not isinstance(draft_data, dict) or not draft_data)
            )
            if not is_empty:
                own_sub_ids_by_uid.setdefault(owner_id, []).append(sid)

        if sub_meta_by_id:
            app_upload_qs = UploadSession.objects.filter(
                requirement__activity=selected_activity,
                requirement__code="application_form",
                status=UploadSession.Status.COMPLETE,
                fileasset__status=FileAsset.Status.VALID,
            )
            if not viewer_is_admin:
                app_upload_qs = app_upload_qs.filter(
                    Q(submission_id__in=sub_meta_by_id.keys())
                    | Q(submission__isnull=True, owner_id__in=scoped_user_ids)
                )
            for up_sub_id, up_owner_id, up_created_at in app_upload_qs.values_list(
                "submission_id", "owner_id", "created_at"
            ):
                target_sid = up_sub_id
                if target_sid is None and owner_sub_counts.get(up_owner_id, 0) <= 1:
                    target_sid = single_sub_by_owner.get(up_owner_id)
                if target_sid is None or target_sid in decl_done_sub_ids:
                    continue
                meta = sub_meta_by_id.get(target_sid)
                if meta is None:
                    continue
                _, _, decl_sub, decl_at, _, _ = meta
                if decl_sub and (decl_at is None or up_created_at <= decl_at):
                    decl_done_sub_ids.add(target_sid)

    summary = {
        "total": len(user_tuples),
        "decl_submitted": 0,
        "decl_unsubmitted": 0,
        "outcomes_submitted": 0,
        "outcomes_unsubmitted": 0,
        "any_unsubmitted": 0,
        "both_unsubmitted": 0,
        "both_submitted": 0,
    }

    parsed_acct_class = parse_class_filter(acct_class_filter) if acct_class_filter else None
    q_lower = acct_q_filter.lower()
    matched_user_ids: list[int] = []

    for uid, username, real_name, grade, major, class_name in user_tuples:
        own_ids = own_sub_ids_by_uid.get(uid, [])
        team_ids = [
            sid
            for sid in team_sub_ids_by_sn.get(username, [])
            if sid in sub_meta_by_id and sub_meta_by_id[sid][0] != uid
        ]

        candidates: list[tuple[uuid.UUID, bool]] = [(sid, False) for sid in own_ids] + [
            (sid, True) for sid in team_ids
        ]

        if candidates:
            candidates.sort(
                key=lambda pair: (
                    1 if sub_meta_by_id[pair[0]][1] in outcome_done_statuses else 0,
                    1 if pair[0] in decl_done_sub_ids else 0,
                    (
                        status_rank_map.get(sub_meta_by_id[pair[0]][1], 0),
                        1 if sub_meta_by_id[pair[0]][2] else 0,
                        sub_meta_by_id[pair[0]][4],
                    ),
                    0 if not pair[1] else -1,
                ),
                reverse=True,
            )
            eff_sid, is_member_pick = candidates[0]
            eff_meta = sub_meta_by_id[eff_sid]
            raw_draft = eff_meta[5] if isinstance(eff_meta[5], dict) else {}
            if is_member_pick:
                participation_type = "team"
            else:
                is_team = raw_draft.get("declaration_type") == Submission.DeclarationType.TEAM or bool(
                    raw_draft.get("team_members")
                )
                participation_type = "team" if is_team else "individual"
            project_name = str(raw_draft.get("project_name") or raw_draft.get("title") or "").strip()
            decl_submitted = any(sid in decl_done_sub_ids for sid, _ in candidates)
            outcomes_submitted = any(sub_meta_by_id[sid][1] in outcome_done_statuses for sid, _ in candidates)
        else:
            participation_type = "none"
            project_name = ""
            decl_submitted = False
            outcomes_submitted = False

        if decl_submitted:
            summary["decl_submitted"] += 1
        else:
            summary["decl_unsubmitted"] += 1
        if outcomes_submitted:
            summary["outcomes_submitted"] += 1
        else:
            summary["outcomes_unsubmitted"] += 1
        if not decl_submitted or not outcomes_submitted:
            summary["any_unsubmitted"] += 1
        if not decl_submitted and not outcomes_submitted:
            summary["both_unsubmitted"] += 1
        if decl_submitted and outcomes_submitted:
            summary["both_submitted"] += 1

        if parsed_acct_class is not None:
            stu_scope = {
                "grade": str(grade or "").strip(),
                "major": str(major or "").strip(),
                "class_name": str(class_name or "").strip(),
            }
            if not scope_matches_class_filter(stu_scope, parsed_acct_class):
                continue
        if acct_decl_type_filter in ("individual", "team", "none") and participation_type != acct_decl_type_filter:
            continue
        if q_lower:
            display_name = (real_name or username or "").lower()
            if (
                q_lower not in (username or "").lower()
                and q_lower not in display_name
                and q_lower not in project_name.lower()
            ):
                continue
        if acct_submit_filter == "any_unsubmitted" and (decl_submitted and outcomes_submitted):
            continue
        elif acct_submit_filter == "decl_unsubmitted" and decl_submitted:
            continue
        elif acct_submit_filter == "outcome_unsubmitted" and outcomes_submitted:
            continue
        elif acct_submit_filter == "both_unsubmitted" and (decl_submitted or outcomes_submitted):
            continue
        elif acct_submit_filter == "decl_only" and not (decl_submitted and not outcomes_submitted):
            continue
        elif acct_submit_filter == "both_submitted" and not (decl_submitted and outcomes_submitted):
            continue

        matched_user_ids.append(uid)

    paginator = Paginator(matched_user_ids, acct_page_size)
    page_obj = paginator.get_page(acct_page_num)
    page_user_ids = list(page_obj.object_list)

    page_rows = []
    if page_user_ids:
        users_by_id = {u.id: u for u in User.objects.filter(id__in=page_user_ids)}
        page_users = [users_by_id[uid] for uid in page_user_ids if uid in users_by_id]
        page_sub_id_set: set[uuid.UUID] = set()
        for stu in page_users:
            page_sub_id_set.update(own_sub_ids_by_uid.get(stu.id, []))
            for sid in team_sub_ids_by_sn.get(stu.username, []):
                if sid in sub_meta_by_id and sub_meta_by_id[sid][0] != stu.id:
                    page_sub_id_set.add(sid)

        page_subs_by_id = (
            {
                sub.id: sub
                for sub in Submission.objects.select_related("owner").filter(id__in=page_sub_id_set)
            }
            if page_sub_id_set
            else {}
        )

        for stu in page_users:
            own_subs = [page_subs_by_id[sid] for sid in own_sub_ids_by_uid.get(stu.id, []) if sid in page_subs_by_id]
            team_subs = [
                page_subs_by_id[sid]
                for sid in team_sub_ids_by_sn.get(stu.username, [])
                if sid in page_subs_by_id and page_subs_by_id[sid].owner_id != stu.id
            ]
            candidates_objs: list[tuple[Submission, bool]] = [(s, False) for s in own_subs] + [
                (s, True) for s in team_subs
            ]
            candidates_objs.sort(
                key=lambda pair: (
                    1 if pair[0].status in outcome_done_statuses else 0,
                    1 if pair[0].id in decl_done_sub_ids else 0,
                    _team_submission_priority(pair[0]),
                    0 if not pair[1] else -1,
                ),
                reverse=True,
            )

            if candidates_objs:
                eff_sub, is_member_pick = candidates_objs[0]
                can_view_submission = bool(
                    viewer_is_admin
                    or is_target_in_reviewer_scope(viewer, eff_sub.owner, activity_id=selected_activity_id)
                )
                if is_member_pick:
                    participation_type = "team"
                    participation_role = "member"
                    leader = eff_sub.owner
                    leader_label = f"{leader.real_name or leader.username}（{leader.username}）" if can_view_submission else ""
                    participation_label = "团队申报（队员）"
                else:
                    raw_draft = eff_sub.draft_data if isinstance(eff_sub.draft_data, dict) else {}
                    is_team = raw_draft.get("declaration_type") == Submission.DeclarationType.TEAM or bool(
                        raw_draft.get("team_members")
                    )
                    participation_type = "team" if is_team else "individual"
                    participation_role = "leader" if is_team else "individual"
                    leader_label = ""
                    participation_label = "团队申报（队长）" if is_team else "个人申报"
                if len(candidates_objs) > 1:
                    participation_label = f"{participation_label} · 共 {len(candidates_objs)} 项"
                raw_draft = eff_sub.draft_data if isinstance(eff_sub.draft_data, dict) else {}
                project_name = str(raw_draft.get("project_name") or raw_draft.get("title") or "").strip()
                decl_submitted = any(sub.id in decl_done_sub_ids for sub, _ in candidates_objs)
                outcomes_submitted = any(sub.status in outcome_done_statuses for sub, _ in candidates_objs)
                status_code = eff_sub.status
                status_label = eff_sub.get_status_display()
                submission_id = str(eff_sub.id) if can_view_submission else ""
            else:
                can_view_submission = False
                participation_type = "none"
                participation_role = "none"
                leader_label = ""
                participation_label = "未申报"
                project_name = ""
                decl_submitted = False
                outcomes_submitted = False
                status_code = "none"
                status_label = "未提交"
                submission_id = ""

            page_rows.append(
                {
                    "student_number": stu.username,
                    "real_name": stu.real_name or stu.username,
                    "class_name": stu.class_name or "-",
                    "class_display": format_user_class_label(stu) or "-",
                    "grade": stu.grade or "",
                    "major": stu.major or "",
                    "phone": stu.phone or "",
                    "participation_type": participation_type,
                    "participation_role": participation_role,
                    "participation_label": participation_label,
                    "team_leader_label": leader_label,
                    "project_name": project_name,
                    "declaration_submitted": decl_submitted,
                    "outcomes_submitted": outcomes_submitted,
                    "status_code": status_code,
                    "status_label": status_label,
                    "submission_id": submission_id,
                    "can_view_submission": can_view_submission,
                }
            )

    return {
        "account_progress_rows": page_rows,
        "account_progress_filtered_count": paginator.count,
        "account_progress_page_obj": page_obj,
        "account_progress_page_range": _elided_page_range(page_obj),
        "account_progress_query_without_page": _query_without_param(request, "acct_page"),
        "account_progress_summary": summary,
        "account_progress_classes": acct_classes,
        "acct_submit_filter": acct_submit_filter,
        "acct_class_filter": acct_class_filter,
        "acct_decl_type_filter": acct_decl_type_filter,
        "acct_q_filter": acct_q_filter,
    }


def _build_reviewer_and_admin_context(request):
    user = request.user
    is_admin = is_college_admin(user)
    activities = list(workbench_activities(user)[:100])

    selected_act_id = request.GET.get("activity_id")
    selected_activity = None
    if selected_act_id:
        for act in activities:
            if str(act.id) == str(selected_act_id):
                selected_activity = act
                break
    if selected_activity is None and activities:
        selected_activity = activities[0]

    status_filter = request.GET.get("status", "").strip()
    class_filter = (request.GET.get("class_name") or request.GET.get("class") or "").strip()
    grade_filter = (request.GET.get("grade") or "").strip()
    major_filter = (request.GET.get("major") or "").strip()
    category_filter = (request.GET.get("category") or "").strip()
    declaration_type_filter = normalize_declaration_type_filter(request.GET.get("declaration_type"))
    selected_submission_id = request.GET.get("submission_id", "").strip()
    sub_page_num = request.GET.get("sub_page") or request.GET.get("page") or 1
    sub_page_size = parse_page_size(
        request.GET.get("sub_page_size") or request.GET.get("page_size"),
        default=DASHBOARD_SUBMISSION_PAGE_SIZE,
        maximum=WORKBENCH_MAX_PAGE_SIZE,
    )

    submission_items = []
    submission_page_obj = None
    available_classes = []
    declared_classes = []
    allowed_reviewer_classes = (
        None
        if is_admin
        else reviewer_allowed_class_labels(
            user, activity_id=selected_activity.id if selected_activity is not None else None
        )
    )
    stats = {
        "total": 0,
        "pending_first_review": 0,
        "pending_final_review": 0,
        "final_approved": 0,
        "returned": 0,
        "draft": 0,
    }

    if selected_activity is not None:
        ensure_standard_material_requirements(selected_activity)
        base_qs = reviewable_submissions(user, selected_activity.id)
        stats = summarize_submission_counts(base_qs)

        if is_admin:
            declared_classes = list_activity_declared_classes(actor=user, activity_id=selected_activity.id)
            user_classes = {
                format_class_scope_label(g, m, c)
                for g, m, c in User.objects.filter(role=User.Role.STUDENT)
                .exclude(class_name="")
                .values_list("grade", "major", "class_name")
                .distinct()
                if str(c or "").strip()
            }
            available_classes = sorted(lbl for lbl in (set(declared_classes) | user_classes) if lbl)
        elif allowed_reviewer_classes:
            declared_classes = list_activity_declared_classes(actor=user, activity_id=selected_activity.id)
            available_classes = sorted(lbl for lbl in (set(declared_classes) | set(allowed_reviewer_classes)) if lbl)
        else:
            declared_classes = []
            available_classes = []

        filtered_qs = base_qs
        if status_filter:
            try:
                resolved_status = resolve_status_filter(status_filter)
                if resolved_status:
                    filtered_qs = filtered_qs.filter(status=resolved_status)
            except DomainError:
                pass
        class_q = build_class_scope_q(class_filter, prefix="owner__", grade=grade_filter, major=major_filter)
        if class_q:
            filtered_qs = filtered_qs.filter(class_q)
        if category_filter:
            filtered_qs = filtered_qs.filter(draft_data__category=category_filter)
        decl_q = declaration_type_filter_q(declaration_type_filter)
        if decl_q is not None:
            filtered_qs = filtered_qs.filter(decl_q)

        paginator = Paginator(
            filtered_qs.select_related("owner")
            .prefetch_related(
                Prefetch(
                    "review_records",
                    queryset=ReviewRecord.objects.select_related("reviewer").order_by("-created_at"),
                    to_attr="prefetched_review_records",
                )
            )
            .order_by("-updated_at", "-created_at"),
            sub_page_size,
        )
        submission_page_obj = paginator.get_page(sub_page_num)
        subs = list(submission_page_obj.object_list)
        uploads_by_submission = (
            map_uploads_by_submission(subs, activity=selected_activity) if subs else {}
        )

        for s in subs:
            serialized = serialize_submission_for_review(s, uploads_by_submission=uploads_by_submission)
            can_first_role = user.role == User.Role.REVIEWER or is_admin
            serialized["can_first_review"] = (
                s.status == Submission.Status.PENDING_FIRST_REVIEW and can_first_role
            )
            serialized["can_final_review"] = (
                s.status == Submission.Status.PENDING_FINAL_REVIEW and is_admin
            )
            serialized["can_return_approved"] = (
                s.status in (Submission.Status.PENDING_FINAL_REVIEW, Submission.Status.FINAL_APPROVED)
                and can_first_role
            )
            serialized["requires_return_confirm"] = s.status in (
                Submission.Status.PENDING_FINAL_REVIEW,
                Submission.Status.FINAL_APPROVED,
            )
            serialized["is_drawer_open"] = str(s.id) == selected_submission_id
            submission_items.append(serialized)

    export_classes = declared_classes if declared_classes else available_classes
    selected_activity_categories = (
        selected_activity.get_project_categories()
        if selected_activity is not None
        else list(DEFAULT_PROJECT_CATEGORIES)
    )
    can_export = bool(selected_activity) and (is_admin or bool(allowed_reviewer_classes))
    reviewer_class_label = "" if is_admin else format_user_class_label(user)

    ctx = {
        "workbench_role": "admin" if is_admin else "reviewer",
        "is_college_admin": is_admin,
        "reviewer_class_label": reviewer_class_label,
        "can_export": can_export,
        "show_account_progress": is_admin or is_reviewer(user),
        "activities": activities,
        "selected_activity": selected_activity,
        "selected_activity_categories": selected_activity_categories,
        "selected_activity_categories_text": "\n".join(selected_activity_categories),
        "default_project_categories_text": "\n".join(DEFAULT_PROJECT_CATEGORIES),
        "selected_activity_has_custom_categories": bool(selected_activity and selected_activity.project_categories),
        "status_filter": status_filter,
        "class_filter": class_filter,
        "category_filter": category_filter,
        "declaration_type_filter": declaration_type_filter,
        "available_classes": available_classes,
        "declared_classes": declared_classes,
        "export_classes": export_classes,
        "stats": stats,
        "submission_items": submission_items,
        "submission_page_obj": submission_page_obj,
        "submission_page_range": _elided_page_range(submission_page_obj),
        "submission_total_count": submission_page_obj.paginator.count if submission_page_obj is not None else 0,
        "submission_query_without_page": _query_without_param(request, "sub_page"),
    }
    if is_admin or is_reviewer(user):
        ctx.update(_build_admin_account_progress(request, selected_activity, actor=user))
    return ctx


@login_required
@require_GET
def dashboard_view(request):
    user = request.user
    if is_college_admin(user) or user.role == User.Role.REVIEWER:
        context = _build_reviewer_and_admin_context(request)
        context["is_admin_panel"] = False
    else:
        context = _build_student_workbench_context(request)
    return render(request, "dashboard.html", context)


def _extract_draft_payload_from_post(request) -> dict:
    project_name = request.POST.get("project_name", "").strip()
    category = request.POST.get("category", "").strip()
    declaration_type = request.POST.get("declaration_type", "individual").strip()
    if declaration_type not in ("individual", "team"):
        declaration_type = "individual"

    team_members = []
    raw_json = request.POST.get("team_members_json", "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, list):
                team_members = parsed
        except ValueError:
            pass
    else:
        sns = request.POST.getlist("member_student_number")
        rns = request.POST.getlist("member_real_name")
        cns = request.POST.getlist("member_class_name")
        for sn, rn, cn in zip(sns, rns, cns):
            sn_s, rn_s, cn_s = sn.strip(), rn.strip(), cn.strip()
            if sn_s or rn_s or cn_s:
                team_members.append(
                    {
                        "student_number": sn_s,
                        "real_name": rn_s,
                        "class_name": cn_s,
                    }
                )

    if declaration_type == "individual":
        team_members = []

    return {
        "project_name": project_name,
        "category": category,
        "declaration_type": declaration_type,
        "team_members": team_members,
    }


@login_required
@require_POST
def student_save_draft_view(request):
    try:
        require_student(request.user)
        activity_id = request.POST.get("activity_id", "").strip()
        submission_id = request.POST.get("submission_id", "").strip()
        if submission_id:
            sub_uuid = _parse_uuid_or_domain_error(submission_id, "submission_id")
            sub = get_object_or_404(Submission, pk=sub_uuid, owner=request.user)
        elif activity_id:
            act_uuid = _parse_uuid_or_domain_error(activity_id, "activity_id")
            sub = create_draft(actor=request.user, activity_id=act_uuid)
        else:
            raise DomainError("缺少活动或申报记录标识。")

        expected_version_raw = request.POST.get("expected_version")
        expected_version = (
            int(expected_version_raw)
            if expected_version_raw is not None and str(expected_version_raw).strip().isdigit()
            else sub.version
        )
        draft_payload = _extract_draft_payload_from_post(request)
        updated = save_draft(
            actor=request.user,
            submission_id=sub.id,
            expected_version=expected_version,
            data=draft_payload,
        )
        messages.success(request, "草稿已成功保存！")
        return redirect(f"/?activity_id={updated.activity_id}&submission_id={updated.id}")
    except DomainError as exc:
        messages.error(request, str(exc))
        return HttpResponseBadRequest(str(exc))


@login_required
@require_POST
def student_submit_review_view(request, submission_id):
    try:
        require_student(request.user)
        sub = get_object_or_404(Submission, pk=submission_id, owner=request.user)
        if "project_name" in request.POST:
            draft_payload = _extract_draft_payload_from_post(request)
            if draft_payload.get("project_name"):
                sub = save_draft(
                    actor=request.user,
                    submission_id=sub.id,
                    expected_version=sub.version,
                    data=draft_payload,
                )
        sub, _ = submit_for_review(actor=request.user, submission_id=sub.id)
        messages.success(request, "申报材料已提交初审。")
        return redirect(f"/?activity_id={sub.activity_id}&submission_id={sub.id}")
    except DomainError as exc:
        messages.error(request, str(exc))
        return HttpResponseBadRequest(str(exc))


@login_required
@require_GET
@require_reviewer_or_admin
def review_submission_detail_view(request, submission_id):
    sub = get_object_or_404(Submission.objects.select_related("owner", "activity"), pk=submission_id)
    if not reviewable_submissions(request.user, sub.activity_id).filter(pk=sub.pk).exists():
        return HttpResponseForbidden("无权查看该申报记录。")
    is_admin = is_college_admin(request.user)
    can_first_role = request.user.role == User.Role.REVIEWER or is_admin
    serialized = serialize_submission_for_review(sub)
    serialized["can_first_review"] = (
        sub.status == Submission.Status.PENDING_FIRST_REVIEW and can_first_role
    )
    serialized["can_final_review"] = (
        sub.status == Submission.Status.PENDING_FINAL_REVIEW and is_admin
    )
    serialized["can_return_approved"] = (
        sub.status in (Submission.Status.PENDING_FINAL_REVIEW, Submission.Status.FINAL_APPROVED)
        and can_first_role
    )
    serialized["requires_return_confirm"] = sub.status in (
        Submission.Status.PENDING_FINAL_REVIEW,
        Submission.Status.FINAL_APPROVED,
    )
    return render(
        request,
        "review_detail.html",
        {
            "submission": sub,
            "item": serialized,
            "is_college_admin": is_admin,
        },
    )


@login_required
@require_POST
@require_reviewer_or_admin
def review_submission_action_view(request, submission_id):
    sub = get_object_or_404(Submission.objects.select_related("owner", "activity"), pk=submission_id)
    decision = request.POST.get("decision", "").strip()
    comment = request.POST.get("comment", "").strip()
    confirm_return = request.POST.get("confirm_return", "").strip().lower() in ("1", "true", "yes", "on")

    try:
        if decision == "returned" and not comment:
            raise DomainError("打回修改时必须填写打回意见，原因不能为空。")
        _, updated_sub = review_submission(
            actor=request.user,
            activity_id=sub.activity_id,
            submission_id=sub.id,
            decision=decision,
            comment=comment,
            confirm_approved_return=confirm_return,
        )
    except Forbidden as exc:
        return HttpResponseForbidden(str(exc))
    except DomainError as exc:
        messages.error(request, str(exc))
        return HttpResponseBadRequest(str(exc))

    messages.success(request, f"审核操作已完成，当前状态更新为【{updated_sub.get_status_display()}】。")
    next_url = request.POST.get("next") or f"/?activity_id={sub.activity_id}"
    return redirect(next_url)


@login_required
@require_POST
@require_college_admin
def admin_save_activity_view(request):
    activity_id = request.POST.get("activity_id", "").strip()
    title = request.POST.get("title", "").strip()
    opens_at_raw = request.POST.get("opens_at", "").strip()
    deadline_raw = request.POST.get("deadline", "").strip()
    is_open = request.POST.get("is_open") in ("on", "true", "1", "yes")
    has_categories_field = "project_categories" in request.POST
    raw_categories = request.POST.get("project_categories", "")

    try:
        opens_at = parse_datetime(opens_at_raw) if opens_at_raw else None
        deadline = parse_datetime(deadline_raw) if deadline_raw else None
        if not opens_at or not deadline:
            raise DomainError("请填写合法的活动开始时间与截止时间。")
        if timezone.is_naive(opens_at):
            opens_at = timezone.make_aware(opens_at, timezone.get_current_timezone())
        if timezone.is_naive(deadline):
            deadline = timezone.make_aware(deadline, timezone.get_current_timezone())
        if deadline <= opens_at:
            raise DomainError("活动截止时间必须晚于开始时间。")

        parsed_categories = None
        if has_categories_field:
            try:
                normalized_categories = parse_project_categories(raw_categories)
            except ValueError as exc:
                raise DomainError(str(exc)) from None
            parsed_categories = (
                [] if normalized_categories == list(DEFAULT_PROJECT_CATEGORIES) else normalized_categories
            )

        if activity_id:
            act_uuid = _parse_uuid_or_domain_error(activity_id, "activity_id")
            act = get_object_or_404(Activity, pk=act_uuid)
            if title:
                act.title = title
            act.opens_at = opens_at
            act.deadline = deadline
            act.is_open = is_open
            if parsed_categories is not None:
                act.project_categories = parsed_categories
            act.save()
            action_name = "activity.updated"
        else:
            if not title:
                raise DomainError("请填写活动名称。")
            act = Activity.objects.create(
                title=title,
                opens_at=opens_at,
                deadline=deadline,
                is_open=is_open,
                project_categories=parsed_categories or [],
            )
            action_name = "activity.created"

        ensure_standard_material_requirements(act, sync_existing=True)
        record(
            actor=request.user,
            action=action_name,
            target=act,
            metadata={
                "title": act.title,
                "is_open": act.is_open,
                "project_categories": act.get_project_categories(),
            },
        )
    except DomainError as exc:
        messages.error(request, str(exc))
        return HttpResponseBadRequest(str(exc))

    messages.success(request, f"活动【{act.title}】配置已保存。")
    return redirect(f"/?activity_id={act.id}")


@login_required
@require_POST
@require_college_admin
def admin_whitelist_action_view(request, activity_id):
    act = get_object_or_404(Activity, pk=activity_id)
    action = request.POST.get("action", "add").strip().lower()
    raw_numbers = request.POST.get("student_numbers") or request.POST.get("student_number") or ""
    numbers = [x.strip() for x in re.split(r"[,，;；\s]+", str(raw_numbers)) if x.strip()]

    if not numbers:
        return HttpResponseBadRequest("请提供有效的补交学生学号。")

    if action == "remove":
        act.remove_from_late_whitelist(*numbers)
        messages.success(request, f"已从补交白名单移除学号：{', '.join(numbers)}")
    else:
        act.add_to_late_whitelist(*numbers)
        messages.success(request, f"已添加补交白名单学号：{', '.join(numbers)}")

    record(
        actor=request.user,
        action=f"activity.whitelist.{action}",
        target=act,
        metadata={"student_numbers": numbers, "late_whitelist_student_numbers": act.late_whitelist_student_numbers},
    )
    return redirect(f"/?activity_id={act.id}")
# =====================================================================
# 端点上锁：/admin-panel/ 与 /reviews/workbench/ 是管理端入口，
# 学生直接敲 URL 必须被 role 装饰器 403 掉，并落一条 access.role_denied 审计。
# =====================================================================

@login_required
@require_GET
@require_college_admin
def admin_panel_view(request):
    """学院管理员专用管理面板入口。"""
    context = _build_reviewer_and_admin_context(request)
    context["is_admin_panel"] = True
    return render(request, "dashboard.html", context)


def _build_review_workbench_context(request):
    """审核工作台：服务端筛选（Q 对象）+ 全历史时间线 + 异步导出任务列表。"""
    from apps.exports.views import list_export_jobs_for_user

    user = request.user
    is_admin = is_college_admin(user)
    activities = list(workbench_activities(user))
    valid_activity_ids = {str(act.id) for act in activities}

    activity_id = (request.GET.get("activity_id") or "").strip()
    if activity_id and activity_id not in valid_activity_ids:
        activity_id = ""
    if not activity_id and activities:
        activity_id = str(activities[0].id)
    selected_activity = next((act for act in activities if str(act.id) == activity_id), None)

    class_filter = (request.GET.get("class_name") or request.GET.get("class") or "").strip()
    grade_filter = (request.GET.get("grade") or "").strip()
    major_filter = (request.GET.get("major") or "").strip()
    category_filter = (request.GET.get("category") or "").strip()
    declaration_type_filter = normalize_declaration_type_filter(request.GET.get("declaration_type"))
    raw_status_filter = (request.GET.get("status") or "").strip()
    if raw_status_filter == Submission.LATE_SUBMITTED:
        status_filter = Submission.LATE_SUBMITTED
    elif raw_status_filter:
        try:
            status_filter = resolve_status_filter(raw_status_filter) or ""
        except DomainError:
            status_filter = ""
    else:
        status_filter = ""
    late_filter = (request.GET.get("late") or "").strip()
    page_num = request.GET.get("page") or 1
    page_size = parse_page_size(
        request.GET.get("page_size"),
        default=WORKBENCH_PAGE_SIZE,
        maximum=WORKBENCH_MAX_PAGE_SIZE,
    )

    whitelist_numbers = workbench_whitelist_numbers(activities=activities, activity_id=activity_id or None)
    if not is_admin and whitelist_numbers:
        scoped_sns = set(
            User.objects.filter(
                reviewer_user_scope_q(
                    user,
                    prefix="",
                    activity_id=selected_activity.id if selected_activity is not None else None,
                ),
                username__in=whitelist_numbers,
            ).values_list("username", flat=True)
        )
        whitelist_numbers = [sn for sn in whitelist_numbers if sn in scoped_sns]
    page_data = collect_workbench_page(
        user=user,
        activity_id=activity_id or None,
        class_name=class_filter,
        grade=grade_filter,
        major=major_filter,
        category=category_filter,
        declaration_type=declaration_type_filter,
        status=status_filter,
        late=late_filter,
        whitelist_numbers=whitelist_numbers,
        page=page_num,
        page_size=page_size,
    )
    rows = page_data["rows"]
    page_obj = page_data["page_obj"]
    counts = page_data["counts"]
    category_options = (
        selected_activity.get_project_categories()
        if selected_activity is not None
        else list(DEFAULT_PROJECT_CATEGORIES)
    )

    status_options = [{"code": code, "label": label, "tone": tone} for code, label, tone in Submission.BADGE_ORDER]

    whitelist_invalid = request.session.pop("late_whitelist_invalid", [])
    late_settings_saved = request.session.pop("late_settings_saved", False)
    whitelist_text = (
        "\n".join(str(sn) for sn in (selected_activity.late_whitelist_student_numbers or []))
        if selected_activity and is_admin
        else ""
    )
    allowed_reviewer_classes = (
        None
        if is_admin
        else reviewer_allowed_class_labels(
            user, activity_id=selected_activity.id if selected_activity is not None else None
        )
    )

    return {
        "workbench_role": "admin" if is_admin else "reviewer",
        "is_college_admin": is_admin,
        "reviewer_class_label": "" if is_admin else format_user_class_label(user),
        "activities": activities,
        "selected_activity": selected_activity,
        "activity_id": activity_id,
        "class_filter": class_filter,
        "category_filter": category_filter,
        "declaration_type_filter": declaration_type_filter,
        "status_filter": status_filter,
        "late_filter": late_filter,
        "class_options": workbench_class_options(user),
        "category_options": category_options,
        "status_options": status_options,
        "rows": rows,
        "page_obj": page_obj,
        "page_range": _elided_page_range(page_obj),
        "workbench_query_without_page": _query_without_param(request, "page"),
        "counts": counts,
        "whitelist_numbers": whitelist_numbers,
        "whitelist_text": whitelist_text,
        "whitelist_invalid": whitelist_invalid,
        "late_settings_saved": late_settings_saved,
        "can_export": bool(selected_activity) and (is_admin or bool(allowed_reviewer_classes)),
        "export_jobs": list_export_jobs_for_user(user),
        "export_root": f"/api/v1/exports/",
    }


@login_required
@require_GET
@require_reviewer_or_admin
def review_workbench_view(request):
    """审核老师/学院管理员专用工作台入口：表格 + 筛选 + 审核历史时间线 + 批量导出。"""
    return render(request, "reviews/workbench.html", _build_review_workbench_context(request))


@login_required
@require_POST
@require_college_admin
def admin_late_settings_view(request, activity_id):
    """补交设置卡片保存：开关 + 补交截止时间 + 批量粘贴学号（去重后整体替换）。"""
    act = get_object_or_404(Activity, pk=activity_id)
    enabled = request.POST.get("late_submission_enabled") in ("on", "true", "1", "yes")
    deadline_raw = request.POST.get("late_submission_deadline", "")
    raw_numbers = request.POST.get("late_whitelist_text") or request.POST.get("student_numbers") or ""

    try:
        result = update_late_submission_settings(
            actor=request.user,
            activity=act,
            enabled=enabled,
            deadline_raw=deadline_raw,
            raw_numbers=raw_numbers,
        )
    except DomainError as exc:
        messages.error(request, str(exc))
        return redirect(f"/reviews/workbench/?activity_id={act.id}")

    request.session["late_settings_saved"] = True
    if result["invalid"]:
        request.session["late_whitelist_invalid"] = result["invalid"]
        messages.warning(
            request,
            f"补交设置已保存：{len(result['valid'])} 个学号写入白名单；"
            f"{len(result['invalid'])} 个学号在 StudentRoster 中不存在，已标红并未写入。",
        )
    else:
        messages.success(
            request,
            f"补交设置已保存：白名单 {len(result['valid'])} 个学号，开关{'已开启' if enabled else '已关闭'}。",
        )
    return redirect(f"/reviews/workbench/?activity_id={act.id}")


@endpoint("POST")
def admin_parse_whitelist_view(request, activity_id):
    """textarea 实时校验：解析 + 去重 + StudentRoster 存在性检查，返回非法学号供前端标红。"""
    if not is_college_admin(request.user):
        raise Forbidden("仅学院管理员可校验补交白名单。")
    Activity.objects.get(pk=activity_id)
    data = json_body(request)
    numbers = parse_student_numbers(data.get("student_numbers") or data.get("late_whitelist_text") or "")
    resolved = resolve_known_student_numbers(numbers)
    return JsonResponse(
        {
            "total": resolved["total"],
            "valid": resolved["valid"],
            "invalid": resolved["invalid"],
            "valid_count": len(resolved["valid"]),
            "invalid_count": len(resolved["invalid"]),
        },
        status=200,
    )
