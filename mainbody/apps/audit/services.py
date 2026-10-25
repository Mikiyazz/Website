from .models import AuditLog


def record(*, actor, action, target, request_id="", metadata=None):
    """Call inside the business transaction. Never include credentials or raw files."""
    return AuditLog.objects.create(actor=actor, action=action, target_type=target._meta.label_lower,
                                   target_id=str(target.pk), request_id=request_id, metadata=metadata or {})
