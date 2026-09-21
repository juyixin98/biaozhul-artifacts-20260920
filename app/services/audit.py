"""Audit helper."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog


def record(
    session: Session,
    *,
    action: str,
    actor_type: str,
    actor_id: str,
    task_id: int | None = None,
    reason: str | None = None,
    detail: dict[str, Any] | None = None,
    now: datetime,
) -> AuditLog:
    entry = AuditLog(
        task_id=task_id,
        actor_type=actor_type,
        actor_id=str(actor_id),
        action=action,
        reason=reason,
        detail=detail or {},
        created_at=now,
    )
    session.add(entry)
    return entry
