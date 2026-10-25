import logging
import time
import uuid

logger = logging.getLogger("request")


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Do not trust arbitrary client header contents; never log body/query/cookies.
        request.request_id = uuid.uuid4().hex
        start = time.monotonic()
        response = self.get_response(request)
        response["X-Request-ID"] = request.request_id
        match = getattr(request, "resolver_match", None)
        logger.info("http_request", extra={
            "request_id": request.request_id, "method": request.method,
            "route": getattr(match, "route", "unresolved"), "status": response.status_code,
            "duration_ms": round((time.monotonic() - start) * 1000, 2),
        })
        return response
