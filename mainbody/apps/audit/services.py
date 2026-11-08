"""审计写入：业务流水与安全拒绝都在这里落库。

原则
----
* 审计写失败永远不能把业务请求搞崩：异常在这里被吞掉并降级成日志。
* 拒绝记录必须带 user / action / target / ip / reason 五个字段。
* IP 默认只取 REMOTE_ADDR；只有 settings.TRUST_X_FORWARDED_FOR 为真才解析代理头，
  且逐跳做 ipaddress 校验，避免伪造 X-Forwarded-For 污染审计。
"""
import ipaddress
import logging

from django.conf import settings

from .models import AuditLog

logger = logging.getLogger(__name__)


def _as_ip(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def client_ip(request) -> str | None:
    """Best-effort client IP for auditing (never trusts user-controlled headers by default)."""
    if request is None:
        return None
    if getattr(settings, "TRUST_X_FORWARDED_FOR", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        hops = [hop.strip() for hop in str(forwarded).split(",") if hop.strip()]
        for hop in reversed(hops):
            parsed = _as_ip(hop)
            if parsed:
                return parsed
    return _as_ip(request.META.get("REMOTE_ADDR", ""))


def _deal_target(target) -> tuple[str, str]:
    if target is None:
        return "", ""
    meta = getattr(target, "_meta", None)
    if meta is not None:
        return meta.label_lower, str(getattr(target, "pk", ""))
    return "raw", str(target)[:64]


def record(*, actor, action, target, request_id="", metadata=None, ip=None, reason="", request=None):
    """Call inside the business transaction. Never include credentials or raw files."""
    target_type, target_id = _deal_target(target)
    return AuditLog.objects.create(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        request_id=request_id or getattr(request, "request_id", ""),
        ip=ip or client_ip(request),
        reason=str(reason or "")[:300],
        metadata=metadata or {},
    )


def record_rejection(*, action, reason, user=None, target="", request=None, metadata=None, ip=None):
    """Write one "we refused this request" row. Never raises: auditing must not break a request."""
    payload = dict(metadata or {})
    match = getattr(request, "resolver_match", None)
    payload.setdefault("route", getattr(match, "route", "") if match else "")
    payload.setdefault("method", getattr(request, "method", "") if request is not None else "")
    actor = user if getattr(user, "is_authenticated", False) else None
    target_type, target_id = _deal_target(target)
    try:
        return AuditLog.objects.create(
            actor=actor,
            action=str(action)[:100],
            target_type=target_type or "http_request",
            target_id=target_id,
            request_id=getattr(request, "request_id", "") if request is not None else "",
            ip=ip or client_ip(request),
            reason=str(reason or "")[:300],
            metadata=payload,
        )
    except Exception:  # pragma: no cover - 数据库故障时不能把请求带崩
        logger.warning(
            "audit_write_failed",
            extra={"action": str(action)[:100], "error_type": "AuditWriteError"},
        )
        return None
