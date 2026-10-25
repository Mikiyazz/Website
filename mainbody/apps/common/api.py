"""Small JSON boundary: same-origin session + CSRF; no csrf_exempt endpoints."""
import json
from functools import wraps
from django.core.exceptions import ObjectDoesNotExist, RequestDataTooBig
from django.db import OperationalError
from django.http import JsonResponse
from .errors import DomainError


def fail(code, message, status, request):
    return JsonResponse({"error": {"code": code, "message": message},
                         "request_id": getattr(request, "request_id", "")}, status=status)


def endpoint(*methods):
    def decorate(fn):
        @wraps(fn)
        def wrapped(request, *args, **kwargs):
            if request.method not in methods:
                response = fail("method_not_allowed", "不支持此请求方式。", 405, request)
                response["Allow"] = ", ".join(methods)
                return response
            if not request.user.is_authenticated or not request.user.is_active:
                return fail("authentication_required", "请先登录。", 401, request)
            try:
                return fn(request, *args, **kwargs)
            except DomainError as exc:
                return fail(exc.code, str(exc), exc.status, request)
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
