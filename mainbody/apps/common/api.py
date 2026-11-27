"""Small JSON boundary: same-origin session + CSRF; no csrf_exempt endpoints."""
import json
from functools import wraps
from pathlib import Path
from urllib.parse import quote
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, RequestDataTooBig, ValidationError
from django.db import OperationalError
from django.http import FileResponse, HttpResponse, JsonResponse
from .errors import DomainError


def build_media_download_response(
    disk_path: Path,
    *,
    filename: str,
    content_type: str,
    inline: bool = False,
):
    """Build a download/preview response under MEDIA_ROOT.

    When NGINX_ACCEL_REDIRECT_ENABLED is True (production default), hands off the
    file transfer to Nginx via X-Accel-Redirect so Gunicorn workers are never tied
    up streaming large videos or export ZIPs. Otherwise streams directly via
    FileResponse for local development and automated tests.
    """
    media_root = Path(settings.MEDIA_ROOT).resolve()
    resolved = Path(disk_path).resolve()
    if media_root not in resolved.parents or not resolved.is_file():
        raise DomainError("请求的文件不存在或存储路径无效。")

    disposition_type = "inline" if inline else "attachment"
    encoded_name = quote(filename)
    disposition_header = f"{disposition_type}; filename*=UTF-8''{encoded_name}"

    if getattr(settings, "NGINX_ACCEL_REDIRECT_ENABLED", False):
        rel_posix = resolved.relative_to(media_root).as_posix()
        prefix = str(getattr(settings, "NGINX_ACCEL_REDIRECT_PREFIX", "/internal-media") or "/internal-media").rstrip("/")
        response = HttpResponse(b"", content_type=content_type)
        response["X-Accel-Redirect"] = f"{prefix}/{quote(rel_posix, safe='/')}"
        response["Content-Disposition"] = disposition_header
        response["X-Content-Type-Options"] = "nosniff"
        return response

    response = FileResponse(
        open(resolved, "rb"),
        as_attachment=not inline,
        filename=filename,
        content_type=content_type,
    )
    if not response.headers.get("Content-Disposition", "").lower().startswith(disposition_type):
        response["Content-Disposition"] = disposition_header
    return response


def fail(code, message, status, request):
    return JsonResponse({"error": {"code": code, "message": message},
                         "request_id": getattr(request, "request_id", "")}, status=status)


def endpoint(*methods, require_auth=True):
    def decorate(fn):
        @wraps(fn)
        def wrapped(request, *args, **kwargs):
            if request.method not in methods:
                response = fail("method_not_allowed", "不支持此请求方式。", 405, request)
                response["Allow"] = ", ".join(methods)
                return response
            if require_auth and (not request.user.is_authenticated or not request.user.is_active):
                return fail("authentication_required", "请先登录。", 401, request)
            try:
                return fn(request, *args, **kwargs)
            except DomainError as exc:
                return fail(exc.code, str(exc), exc.status, request)
            except ValidationError:
                return fail("invalid_parameter", "请求参数格式不正确。", 400, request)
            except ObjectDoesNotExist:
                return fail("not_found", "记录不存在或无权访问。", 404, request)
            except RequestDataTooBig:
                return fail("payload_too_large", "请求过大；文件请使用直传接口。", 413, request)
            except OperationalError:
                response = fail("temporarily_unavailable", "系统繁忙，请稍后重试。", 503, request)
                response["Retry-After"] = "3"
                return response
        return wrapped
    return decorate


def json_body(request):
    if request.content_type != "application/json":
        raise DomainError("请求必须为 application/json。")
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeError):
        raise DomainError("JSON 内容格式不正确。") from None
    if not isinstance(data, dict):
        raise DomainError("请求内容必须为 JSON 对象。")
    return data
