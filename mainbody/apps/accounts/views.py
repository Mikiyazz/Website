"""Accounts and workbench views: registration, password change, and role-based web workbenches."""
import json
import re
import uuid
from django.contrib import messages
from django.contrib.auth import login as auth_login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from apps.accounts.models import User
from apps.accounts.permissions import is_college_admin, require_student
from apps.activities.models import Activity
from apps.activities.selectors import eligible_activities, is_activity_open_for_user
from apps.audit.services import record
from apps.common.api import endpoint, json_body
from apps.common.errors import DomainError, Forbidden
from apps.exports.services import list_activity_declared_classes, resolve_status_filter
from apps.reviews.selectors import reviewable_submissions
from apps.reviews.services import review_submission
from apps.reviews.views import serialize_submission_for_review
from apps.submissions.models import Submission
from apps.submissions.services import create_draft, save_draft, submit_for_review
from apps.uploads.models import FileAsset, UploadSession
from apps.uploads.policies import INLINE_PREVIEW_EXTENSIONS, STANDARD_MATERIAL_RULES
from apps.uploads.services import ensure_standard_material_requirements
from .services import change_password, register_student


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
# HTML Page Views: Registration & Password Change
# =====================================================================

@require_http_methods(["GET", "POST"])
def register_page_view(request):
    grade_choices = User.Grade.choices
    if request.method == "GET":
        return render(
            request,
            "registration/register.html",
            {
                "grade_choices": grade_choices,
                "form_data": {},
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
        return render(
            request,
            "registration/register.html",
            {
                "grade_choices": grade_choices,
                "form_data": form_data,
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

    messages.success(request, "密码修改成功！首次改密提醒已解除。")
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
    eligible_qs = eligible_activities(user)
    submitted_act_ids = list(
        Submission.objects.filter(owner=user).values_list("activity_id", flat=True)
    )
    all_acts = list(Activity.objects.all().order_by("-created_at")[:100])
    eligible_id_set = set(eligible_qs.values_list("id", flat=True)) | set(submitted_act_ids)
    for act in all_acts:
        if act.is_in_late_whitelist(user.username):
            eligible_id_set.add(act.id)

    activities = [act for act in all_acts if act.id in eligible_id_set]
    submissions_by_act = {
        sub.activity_id: sub
        for sub in Submission.objects.filter(owner=user, activity__in=activities)
    }

    selected_act_id = request.GET.get("activity_id")
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
        can_write_now, window_label = is_activity_open_for_user(act, user)
        in_whitelist = act.is_in_late_whitelist(user.username)
        status_code = sub.status if sub else Submission.Status.DRAFT
        status_display = sub.get_status_display() if sub else "草稿"
        activity_cards.append(
            {
                "activity": act,
                "submission": sub,
                "status_code": status_code,
                "status_display": status_display,
                "can_write_now": can_write_now,
                "window_label": window_label,
                "in_whitelist": in_whitelist,
                "is_selected": bool(selected_activity and act.id == selected_activity.id),
            }
        )

    submission = None
    is_locked = True
    lock_reason = ""
    is_returned = False
    latest_returned_record = None
    review_records = []
    draft_data = {
        "project_name": "",
        "category": "社会调查",
        "declaration_type": "individual",
        "team_members": [],
    }
    material_sections = []

    if selected_activity is not None:
        reqs_map = ensure_standard_material_requirements(selected_activity)
        can_write_now, window_label = is_activity_open_for_user(selected_activity, user)
        submission = submissions_by_act.get(selected_activity.id)
        if submission is None and can_write_now:
            submission = create_draft(actor=user, activity_id=selected_activity.id)
            submissions_by_act[selected_activity.id] = submission

        if submission is not None:
            raw_draft = submission.draft_data if isinstance(submission.draft_data, dict) else {}
            team_members = raw_draft.get("team_members") if isinstance(raw_draft.get("team_members"), list) else []
            decl_type = str(raw_draft.get("declaration_type") or "").strip()
            if decl_type not in ("individual", "team"):
                decl_type = "team" if team_members else "individual"
            draft_data = {
                "project_name": str(raw_draft.get("project_name") or raw_draft.get("title") or "").strip(),
                "category": str(raw_draft.get("category") or "社会调查").strip() or "社会调查",
                "declaration_type": decl_type,
                "team_members": team_members,
            }
            review_records = list(
                submission.review_records.select_related("reviewer").order_by("-created_at")
            )
            if submission.status == Submission.Status.RETURNED:
                is_returned = True
                latest_returned_record = (
                    submission.review_records.filter(decision="returned")
                    .select_related("reviewer")
                    .order_by("-created_at")
                    .first()
                )
            if submission.status in (
                Submission.Status.PENDING_FIRST_REVIEW,
                Submission.Status.PENDING_FINAL_REVIEW,
                Submission.Status.FINAL_APPROVED,
            ):
                is_locked = True
                lock_reason = f"当前申报状态为【{submission.get_status_display()}】，表单与材料上传区已只读锁定。"
            elif not can_write_now:
                is_locked = True
                lock_reason = f"当前活动{window_label}，表单与材料上传区已只读锁定。"
            else:
                is_locked = False
        else:
            is_locked = True
            lock_reason = f"当前活动{window_label}，暂不可创建或修改申报。"

        active_uploads = list(
            UploadSession.objects.select_related("requirement", "fileasset")
            .filter(
                owner=user,
                requirement__activity=selected_activity,
                status=UploadSession.Status.COMPLETE,
                fileasset__status=FileAsset.Status.VALID,
            )
            .order_by("created_at")
        )
        uploads_by_req = {}
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

    return {
        "workbench_role": "student",
        "activity_cards": activity_cards,
        "selected_activity": selected_activity,
        "submission": submission,
        "status_code": submission.status if submission else Submission.Status.DRAFT,
        "status_display": submission.get_status_display() if submission else "草稿",
        "is_locked": is_locked,
        "lock_reason": lock_reason,
        "is_returned": is_returned,
        "latest_returned_record": latest_returned_record,
        "review_records": review_records,
        "draft_data": draft_data,
        "team_members_json": json.dumps(draft_data["team_members"], ensure_ascii=False),
        "material_sections": material_sections,
    }


def _build_reviewer_and_admin_context(request):
    user = request.user
    is_admin = is_college_admin(user)
    activities = list(Activity.objects.all().order_by("-created_at")[:100])

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
    selected_submission_id = request.GET.get("submission_id", "").strip()

    submission_items = []
    available_classes = []
    declared_classes = []
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
        base_qs = reviewable_submissions(user, selected_activity.id).select_related("owner")
        for s in base_qs:
            stats["total"] += 1
            if s.status in stats:
                stats[s.status] += 1

        declared_classes = list_activity_declared_classes(actor=user, activity_id=selected_activity.id)
        user_classes = list(
            User.objects.filter(role=User.Role.STUDENT)
            .exclude(class_name="")
            .order_by("class_name")
            .values_list("class_name", flat=True)
            .distinct()
        )
        available_classes = sorted(set(declared_classes) | set(user_classes))

        filtered_qs = base_qs
        if status_filter:
            try:
                resolved_status = resolve_status_filter(status_filter)
                if resolved_status:
                    filtered_qs = filtered_qs.filter(status=resolved_status)
            except DomainError:
                pass
        if class_filter:
            filtered_qs = filtered_qs.filter(owner__class_name=class_filter)

        subs = list(filtered_qs.order_by("-updated_at")[:100])
        owner_ids = [s.owner_id for s in subs]
        uploads_by_owner = {}
        if owner_ids:
            for up in (
                UploadSession.objects.select_related("requirement", "fileasset")
                .filter(
                    owner_id__in=owner_ids,
                    requirement__activity=selected_activity,
                    status=UploadSession.Status.COMPLETE,
                    fileasset__status=FileAsset.Status.VALID,
                )
                .order_by("created_at")
            ):
                uploads_by_owner.setdefault(up.owner_id, []).append(up)

        for s in subs:
            serialized = serialize_submission_for_review(s, uploads_by_owner=uploads_by_owner)
            serialized["can_first_review"] = s.status == Submission.Status.PENDING_FIRST_REVIEW and (
                user.role == User.Role.REVIEWER or is_admin
            )
            serialized["can_final_review"] = s.status == Submission.Status.PENDING_FINAL_REVIEW and is_admin
            serialized["is_drawer_open"] = str(s.id) == selected_submission_id
            submission_items.append(serialized)

    export_classes = declared_classes if declared_classes else available_classes

    return {
        "workbench_role": "admin" if is_admin else "reviewer",
        "is_college_admin": is_admin,
        "activities": activities,
        "selected_activity": selected_activity,
        "status_filter": status_filter,
        "class_filter": class_filter,
        "available_classes": available_classes,
        "declared_classes": declared_classes,
        "export_classes": export_classes,
        "stats": stats,
        "submission_items": submission_items,
    }


@login_required
@require_GET
def dashboard_view(request):
    user = request.user
    if is_college_admin(user) or user.role == User.Role.REVIEWER:
        context = _build_reviewer_and_admin_context(request)
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
            sub = get_object_or_404(Submission, pk=submission_id, owner=request.user)
        elif activity_id:
            sub = create_draft(actor=request.user, activity_id=uuid.UUID(activity_id))
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
        return redirect(f"/?activity_id={updated.activity_id}")
    except DomainError as exc:
        messages.error(request, str(exc))
        return HttpResponseBadRequest(str(exc))


@login_required
@require_POST
def student_submit_review_view(request, submission_id):
    try:
        require_student(request.user)
        sub = get_object_or_404(Submission, pk=submission_id, owner=request.user)
        if "project_name" in request.POST and sub.status in (Submission.Status.DRAFT, Submission.Status.RETURNED):
            draft_payload = _extract_draft_payload_from_post(request)
            if draft_payload.get("project_name"):
                sub = save_draft(
                    actor=request.user,
                    submission_id=sub.id,
                    expected_version=sub.version,
                    data=draft_payload,
                )
        sub, _ = submit_for_review(actor=request.user, submission_id=sub.id)
        messages.success(request, "申报材料已正式提交初审！当前表单已只读锁定。")
        return redirect(f"/?activity_id={sub.activity_id}")
    except DomainError as exc:
        messages.error(request, str(exc))
        return HttpResponseBadRequest(str(exc))


@login_required
@require_GET
def review_submission_detail_view(request, submission_id):
    sub = get_object_or_404(Submission.objects.select_related("owner", "activity"), pk=submission_id)
    if not reviewable_submissions(request.user, sub.activity_id).filter(pk=sub.pk).exists():
        return HttpResponseForbidden("无权查看该申报记录。")
    is_admin = is_college_admin(request.user)
    serialized = serialize_submission_for_review(sub)
    serialized["can_first_review"] = sub.status == Submission.Status.PENDING_FIRST_REVIEW and (
        request.user.role == User.Role.REVIEWER or is_admin
    )
    serialized["can_final_review"] = sub.status == Submission.Status.PENDING_FINAL_REVIEW and is_admin
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
def review_submission_action_view(request, submission_id):
    sub = get_object_or_404(Submission.objects.select_related("owner", "activity"), pk=submission_id)
    decision = request.POST.get("decision", "").strip()
    comment = request.POST.get("comment", "").strip()

    try:
        if decision == "returned" and not comment:
            raise DomainError("打回修改时必须填写打回意见，原因不能为空。")
        _, updated_sub = review_submission(
            actor=request.user,
            activity_id=sub.activity_id,
            submission_id=sub.id,
            decision=decision,
            comment=comment,
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
def admin_save_activity_view(request):
    if not is_college_admin(request.user):
        return HttpResponseForbidden("仅学院管理员可执行此操作。")

    activity_id = request.POST.get("activity_id", "").strip()
    title = request.POST.get("title", "").strip()
    opens_at_raw = request.POST.get("opens_at", "").strip()
    deadline_raw = request.POST.get("deadline", "").strip()
    is_open = request.POST.get("is_open") in ("on", "true", "1", "yes")

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

        if activity_id:
            act = get_object_or_404(Activity, pk=activity_id)
            if title:
                act.title = title
            act.opens_at = opens_at
            act.deadline = deadline
            act.is_open = is_open
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
            )
            action_name = "activity.created"

        ensure_standard_material_requirements(act)
        record(actor=request.user, action=action_name, target=act, metadata={"title": act.title, "is_open": act.is_open})
    except DomainError as exc:
        messages.error(request, str(exc))
        return HttpResponseBadRequest(str(exc))

    messages.success(request, f"活动【{act.title}】配置已保存。")
    return redirect(f"/?activity_id={act.id}")


@login_required
@require_POST
def admin_whitelist_action_view(request, activity_id):
    if not is_college_admin(request.user):
        return HttpResponseForbidden("仅学院管理员可执行此操作。")

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
