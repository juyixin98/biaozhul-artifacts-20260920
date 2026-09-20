from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AuditLog, User
from ..security import get_current_user

router = APIRouter(tags=["audit"])


@router.get("/audit-log")
def list_audit_log(limit: int = 100, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    limit = min(max(limit, 1), 1000)
    rows = db.scalars(
        select(AuditLog)
        .where(AuditLog.org_id == user.org_id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    ).all()
    return {"entries": [
        {
            "id": str(r.id),
            "actor_id": str(r.actor_id) if r.actor_id else None,
            "actor_role": r.actor_role,
            "action": r.action,
            "resource_type": r.resource_type,
            "resource_id": r.resource_id,
            "detail": r.detail,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]}
