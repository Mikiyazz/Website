"""拒绝动作的审计装饰器：把 DomainError / ObjectDoesNotExist 统一记成审计流水。"""
from functools import wraps

from django.core.exceptions import ObjectDoesNotExist

from apps.common.errors import DomainError

from .services import record_rejection

TARGET_KEYS = ("upload_id", "submission_id", "activity_id", "requirement_id")


def rejection_target(request, kwargs) -> str:
    """Pick the most specific identifier available for the audit row."""
    for key in TARGET_KEYS:
        value = kwargs.get(key) if kwargs else None
        if value:
            return str(value)
    for key in ("upload_id", "submission_id"):
        value = request.POST.get(key) or request.GET.get(key)
        if value:
            return str(value)
    return ""


def audit_rejections(action: str):
    """Record every refused request on this view, then let the original error bubble up.

    Applied inside @endpoint so the audit row is written after the service transaction
    rolled back (otherwise a 403 inside an atomic block would be rolled back with it).
    """

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            try:
                return view(request, *args, **kwargs)
            except (DomainError, ObjectDoesNotExist) as exc:
                code = getattr(exc, "code", exc.__class__.__name__)
                record_rejection(
                    action=action,
                    reason=str(exc) or code,
                    user=getattr(request, "user", None),
                    target=rejection_target(request, kwargs),
                    request=request,
                    metadata={"code": code, "view": getattr(view, "__name__", "")},
                )
                raise

        return wrapper

    return decorator
