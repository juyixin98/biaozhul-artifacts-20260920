from apps.audit.models import AuditLog


def record(action, *, actor=None, target="", detail=None, ip_address=None):
    return AuditLog.objects.create(
        action=action,
        actor=actor,
        target=target,
        detail=detail or {},
        ip_address=ip_address,
    )
