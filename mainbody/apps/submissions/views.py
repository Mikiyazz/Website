import uuid
from django.http import JsonResponse
from apps.accounts.permissions import require_student
from apps.common.api import endpoint, json_body
from apps.common.errors import DomainError
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
def detail(request, submission_id):
    require_student(request.user)
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
