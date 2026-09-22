"""Audit helper -- single entry point for writing audit entries."""
from __future__ import annotations

from typing import Any

from .models import AuditLog


def record_audit(
    *,
    developer,
    action: str,
    resource_type: str,
    resource_id: Any,
    resource_repr: str = "",
    diff: dict | None = None,
) -> AuditLog:
    return AuditLog.objects.create(
        developer=developer,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id),
        resource_repr=resource_repr[:255],
        diff=diff or {},
    )
