from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from redis import Redis


@require_GET
def live(request):
    """Process liveness only; do not restart healthy web workers for a DB outage."""
    return JsonResponse({"status": "alive"})


@require_GET
def ready(request):
    """Infrastructure readiness; does not claim storage/business readiness."""
    checks = {"database": False, "broker": False}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        checks["database"] = True
    except Exception:
        pass
    try:
        with Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=1, socket_timeout=1) as client:
            checks["broker"] = bool(client.ping())
    except Exception:
        pass
    return JsonResponse({"status": "ready" if all(checks.values()) else "unavailable", "checks": checks},
                        status=200 if all(checks.values()) else 503)
