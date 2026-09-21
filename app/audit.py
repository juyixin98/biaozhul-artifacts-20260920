from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog

# Results
OK = "success"
DENIED = "denied"
ERROR = "error"


def _summary(payload: dict[str, Any] | None) -> str | None:
    """Serialize a transaction summary. Only safe fields are passed in."""
    if payload is None:
        return None
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)


def record(
    db: Session,
    *,
    user_id: str | None,
    action: str,
    result: str,
    request_id: str | None = None,
    wallet_id: str | None = None,
    sign_request_id: str | None = None,
    summary: dict[str, Any] | None = None,
    detail: str | None = None,
) -> AuditLog:
    """Append an audit row. Committed by the caller's transaction."""
    import uuid

    if detail is not None and len(detail) > 500:
        detail = detail[:500]
    row = AuditLog(
        id=str(uuid.uuid4()),
        user_id=user_id,
        actor_request_id=request_id,
        action=action,
        result=result,
        wallet_id=str(wallet_id) if wallet_id is not None else None,
        sign_request_id=str(sign_request_id) if sign_request_id is not None else None,
        summary=_summary(summary),
        detail=detail,
    )
    db.add(row)
    return row
