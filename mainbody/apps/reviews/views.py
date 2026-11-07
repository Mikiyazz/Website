"""Reviews views: review actions and queue query endpoints."""
from pathlib import Path
import uuid
from django.http import JsonResponse
from apps.common.api import endpoint, json_body
from apps.common.errors import DomainError, Forbidden
from apps.exports.services import resolve_status_filter
from apps.submissions.models import Submission
from apps.uploads.models import FileAsset, UploadSession
from apps.uploads.policies import INLINE_PREVIEW_EXTENSIONS
from .selectors import reviewable_submissions
from .services import review_submission


def serialize_submission_for_review(sub: Submission, uploads_by_owner=None):
    draft = sub.draft_data if isinstance(sub.draft_data, dict) else {}
    project_name = str(draft.get("project_name") or draft.get("title") or "").strip()
    category = str(draft.get("category") or "").strip()
    declaration_type = str(draft.get("declaration_type") or "individual").strip()
    team_members = draft.get("team_members") if isinstance(draft.get("team_members"), list) else []

    if uploads_by_owner is not None:
        user_uploads = uploads_by_owner.get(sub.owner_id, [])
    else:
        user_uploads = list(
            UploadSession.objects.select_related("requirement", "fileasset")
            .filter(
                owner_id=sub.owner_id,
                requirement__activity_id=sub.activity_id,
                status=UploadSession.Status.COMPLETE,
                fileasset__status=FileAsset.Status.VALID,
            )
            .order_by("created_at")
        )

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
        for rec in sub.review_records.select_related("reviewer").order_by("-created_at")
    ]

    return {
        "id": str(sub.id),
        "activity_id": str(sub.activity_id),
        "student_number": sub.owner.username,
        "real_name": sub.owner.real_name,
        "class_name": sub.owner.class_name,
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

    record_obj, submission = review_submission(
        actor=request.user,
        activity_id=activity_id,
        submission_id=submission_id,
        decision=decision,
        comment=comment,
        revision_id=revision_id,
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

    qs = reviewable_submissions(request.user, activity_id).select_related("owner")
    status_param = request.GET.get("status")
    if status_param and str(status_param).strip():
        resolved_status = resolve_status_filter(status_param)
        if resolved_status:
            qs = qs.filter(status=resolved_status)

    class_param = request.GET.get("class_name") or request.GET.get("class")
    if class_param and str(class_param).strip():
        qs = qs.filter(owner__class_name=str(class_param).strip())

    submissions = list(qs.order_by("-updated_at")[:100])
    owner_ids = [s.owner_id for s in submissions]
    uploads_by_owner = {}
    if owner_ids:
        for up in (
            UploadSession.objects.select_related("requirement", "fileasset")
            .filter(
                owner_id__in=owner_ids,
                requirement__activity_id=activity_id,
                status=UploadSession.Status.COMPLETE,
                fileasset__status=FileAsset.Status.VALID,
            )
            .order_by("created_at")
        ):
            uploads_by_owner.setdefault(up.owner_id, []).append(up)

    items = [serialize_submission_for_review(s, uploads_by_owner=uploads_by_owner) for s in submissions]
    return JsonResponse({"items": items, "limit": 100})


@endpoint("GET")
def review_detail(request, submission_id):
    sub = Submission.objects.select_related("owner", "activity").get(pk=submission_id)
    if not reviewable_submissions(request.user, sub.activity_id).filter(pk=sub.pk).exists():
        raise Forbidden("无权查看该申报记录。")
    return JsonResponse({"submission": serialize_submission_for_review(sub)}, status=200)
