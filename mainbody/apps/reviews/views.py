"""Reviews views: review actions and queue query endpoints."""
from pathlib import Path
import uuid
from django.core.paginator import Paginator
from django.db.models import Prefetch
from django.http import JsonResponse
from apps.accounts.permissions import build_class_scope_q, format_user_class_label
from apps.common.api import endpoint, json_body
from apps.common.errors import DomainError, Forbidden
from apps.exports.services import resolve_status_filter
from apps.submissions.models import Submission
from apps.uploads.policies import INLINE_PREVIEW_EXTENSIONS
from apps.uploads.services import map_uploads_by_submission, uploads_for_submission
from .models import ReviewRecord
from .selectors import declaration_type_filter_q, parse_page_size, reviewable_submissions
from .services import has_first_review_permission, review_submission

REVIEW_API_DEFAULT_PAGE_SIZE = 50
REVIEW_API_MAX_PAGE_SIZE = 50


def serialize_submission_for_review(sub: Submission, uploads_by_owner=None, uploads_by_submission=None):
    draft = sub.draft_data if isinstance(sub.draft_data, dict) else {}
    project_name = str(draft.get("project_name") or draft.get("title") or "").strip()
    category = str(draft.get("category") or "").strip()
    declaration_type = str(draft.get("declaration_type") or "individual").strip()
    team_members = draft.get("team_members") if isinstance(draft.get("team_members"), list) else []

    if uploads_by_submission is not None:
        user_uploads = uploads_by_submission.get(sub.id, [])
    elif uploads_by_owner is not None:
        user_uploads = uploads_by_owner.get(sub.owner_id, [])
    else:
        user_uploads = uploads_for_submission(sub)

    attachments = []
    for up in user_uploads:
        ext = Path(up.original_name).suffix.lower()
        can_inline = ext in INLINE_PREVIEW_EXTENSIONS
        asset = getattr(up, "fileasset", None)
        attachments.append(
            {
                "id": str(up.id),
                "requirement_id": str(up.requirement_id),
                "requirement_code": up.requirement.code,
                "requirement_title": up.requirement.title,
                "original_name": up.original_name,
                "extension": ext,
                "actual_bytes": asset.actual_bytes if asset else up.declared_bytes,
                "can_inline_preview": can_inline,
                "preview_url": f"/api/v1/uploads/{up.id}/file/?inline=1" if can_inline else "",
                "download_url": f"/api/v1/uploads/{up.id}/file/",
            }
        )

    prefetched_records = getattr(sub, "prefetched_review_records", None)
    if prefetched_records is not None:
        review_record_objs = prefetched_records
    else:
        review_record_objs = sub.review_records.select_related("reviewer").order_by("-created_at")

    records = [
        {
            "id": str(rec.id),
            "stage": rec.stage,
            "stage_display": rec.get_stage_display(),
            "decision": rec.decision,
            "decision_display": rec.get_decision_display(),
            "comment": rec.comment,
            "reviewer_name": rec.reviewer.real_name or rec.reviewer.username,
            "created_at": rec.created_at.isoformat(),
        }
        for rec in review_record_objs
    ]

    return {
        "id": str(sub.id),
        "activity_id": str(sub.activity_id),
        "student_number": sub.owner.username,
        "real_name": sub.owner.real_name,
        "class_name": sub.owner.class_name,
        "class_display": format_user_class_label(sub.owner) or "-",
        "grade": sub.owner.grade,
        "major": sub.owner.major,
        "phone": sub.owner.phone,
        "project_name": project_name,
        "category": category,
        "declaration_type": declaration_type,
        "declaration_type_display": "团队申报" if declaration_type == "team" else "个人申报",
        "team_members": team_members,
        "status": sub.status,
        "status_display": sub.get_status_display(),
        "version": sub.version,
        "updated_at": sub.updated_at.isoformat(),
        "attachments": attachments,
        "review_records": records,
    }


@endpoint("POST")
def review(request, submission_id):
    data = json_body(request)
    activity_id_raw = data.get("activity_id")
    if activity_id_raw:
        try:
            activity_id = uuid.UUID(str(activity_id_raw))
        except ValueError:
            raise DomainError("activity_id 必须为有效 UUID。") from None
    else:
        sub_obj = Submission.objects.filter(pk=submission_id).first()
        if not sub_obj:
            raise DomainError("缺少必要参数 activity_id。")
        activity_id = sub_obj.activity_id

    decision = data.get("decision")
    comment = data.get("comment", "")
    revision_id = data.get("revision_id")
    raw_confirm = data.get("confirm_approved_return") if "confirm_approved_return" in data else data.get("confirm_return")
    if isinstance(raw_confirm, bool):
        confirm_approved_return = raw_confirm
    else:
        confirm_approved_return = str(raw_confirm or "").strip().lower() in ("1", "true", "yes", "on")

    record_obj, submission = review_submission(
        actor=request.user,
        activity_id=activity_id,
        submission_id=submission_id,
        decision=decision,
        comment=comment,
        revision_id=revision_id,
        confirm_approved_return=confirm_approved_return,
    )

    return JsonResponse(
        {
            "review_record": {
                "id": str(record_obj.id),
                "stage": record_obj.stage,
                "decision": record_obj.decision,
                "comment": record_obj.comment,
                "created_at": record_obj.created_at.isoformat(),
            },
            "submission_status": submission.status,
            "status_display": submission.get_status_display(),
        },
        status=200,
    )


@endpoint("GET")
def review_list(request):
    activity_id_raw = request.GET.get("activity_id")
    if not activity_id_raw:
        raise DomainError("缺少必要参数 activity_id。")
    try:
        activity_id = uuid.UUID(str(activity_id_raw))
    except ValueError:
        raise DomainError("activity_id 必须为有效 UUID。") from None

    qs = (
        reviewable_submissions(request.user, activity_id)
        .select_related("owner")
        .prefetch_related(
            Prefetch(
                "review_records",
                queryset=ReviewRecord.objects.select_related("reviewer").order_by("-created_at"),
                to_attr="prefetched_review_records",
            )
        )
    )
    status_param = request.GET.get("status")
    if status_param and str(status_param).strip():
        resolved_status = resolve_status_filter(status_param)
        if resolved_status:
            qs = qs.filter(status=resolved_status)

    class_param = request.GET.get("class_name") or request.GET.get("class")
    grade_param = request.GET.get("grade")
    major_param = request.GET.get("major")
    class_q = build_class_scope_q(class_param, prefix="owner__", grade=grade_param, major=major_param)
    if class_q:
        qs = qs.filter(class_q)

    category_param = request.GET.get("category")
    if category_param and str(category_param).strip():
        qs = qs.filter(draft_data__category=str(category_param).strip())

    decl_q = declaration_type_filter_q(request.GET.get("declaration_type"))
    if decl_q is not None:
        qs = qs.filter(decl_q)

    page_size = parse_page_size(
        request.GET.get("page_size") or request.GET.get("limit"),
        default=REVIEW_API_DEFAULT_PAGE_SIZE,
        maximum=REVIEW_API_MAX_PAGE_SIZE,
    )
    paginator = Paginator(qs.order_by("-updated_at", "-created_at"), page_size)
    page_obj = paginator.get_page(request.GET.get("page") or 1)
    submissions = list(page_obj.object_list)
    uploads_by_submission = (
        map_uploads_by_submission(submissions, activity=activity_id) if submissions else {}
    )

    items = [serialize_submission_for_review(s, uploads_by_submission=uploads_by_submission) for s in submissions]
    return JsonResponse(
        {
            "items": items,
            "page": page_obj.number,
            "page_size": page_size,
            "total": paginator.count,
            "total_pages": paginator.num_pages,
            "has_next": page_obj.has_next(),
            "has_previous": page_obj.has_previous(),
            "next_page": page_obj.next_page_number() if page_obj.has_next() else None,
            "previous_page": page_obj.previous_page_number() if page_obj.has_previous() else None,
            "limit": page_size,
        }
    )


@endpoint("GET")
def review_detail(request, submission_id):
    sub = (
        Submission.objects.select_related("owner", "activity")
        .prefetch_related(
            Prefetch(
                "review_records",
                queryset=ReviewRecord.objects.select_related("reviewer").order_by("-created_at"),
                to_attr="prefetched_review_records",
            )
        )
        .get(pk=submission_id)
    )
    if not has_first_review_permission(request.user, sub.activity_id, sub):
        raise Forbidden("无权查看该申报记录。")
    return JsonResponse({"submission": serialize_submission_for_review(sub)}, status=200)
