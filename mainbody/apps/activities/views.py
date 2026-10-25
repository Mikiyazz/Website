from django.http import JsonResponse
from apps.common.api import endpoint
from apps.accounts.permissions import require_student
from .selectors import eligible_activities


@endpoint("GET")
def activity_list(request):
    require_student(request.user)
    rows = list(eligible_activities(request.user).values("id", "title", "opens_at", "deadline", "is_open")[:100])
    return JsonResponse({"items": rows, "limit": 100})
