import uuid
from django.http import JsonResponse
from apps.accounts.permissions import require_student
from apps.common.api import endpoint, json_body
from apps.common.errors import DomainError
from .models import Submission
from .services import create_draft, save_draft, submit_for_review


def serialize(item):
    return {"id": item.id, "activity_id": item.activity_id, "status": item.status,
            "version": item.version, "draft_data": item.draft_data}


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
        item = save_draft(actor=request.user, submission_id=submission_id,
                          expected_version=data.get("expected_version"), data=data.get("draft_data"))
    return JsonResponse(serialize(item))


@endpoint("POST")
def submit(request, submission_id):
    submit_for_review(actor=request.user, submission_id=submission_id)
