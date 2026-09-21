from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.access import alert_visibility_condition, ensure_can_view_alert
from ..core.triage import triage_alert
from ..database import get_db
from ..models import Alert, AlertStatus, User, UserRole
from ..schemas import AlertOut, TriageRequest
from ..security import require_roles

router = APIRouter(prefix="/alerts", tags=["alerts"])

_VIEWERS = (UserRole.admin, UserRole.analyst, UserRole.manager)


@router.get("", response_model=list[AlertOut])
def list_alerts(
    status_filter: AlertStatus | None = Query(default=None, alias="status"),
    user_id: int | None = None,
    rule: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    viewer: User = Depends(require_roles(*_VIEWERS)),
):
    stmt = select(Alert).order_by(Alert.created_at.desc(), Alert.id.desc())
    cond = alert_visibility_condition(viewer, db)
    if cond is not None:
        stmt = stmt.where(cond)
    if status_filter is not None:
        stmt = stmt.where(Alert.status == status_filter)
    if user_id is not None:
        stmt = stmt.where(Alert.user_id == user_id)
    if rule is not None:
        stmt = stmt.where(Alert.rule == rule)
    stmt = stmt.limit(limit).offset(offset)
    return list(db.scalars(stmt).all())


@router.get("/{alert_id}", response_model=AlertOut)
def get_alert(
    alert_id: int,
    db: Session = Depends(get_db),
    viewer: User = Depends(require_roles(*_VIEWERS)),
):
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    ensure_can_view_alert(db, viewer, alert)
    return alert


@router.post("/{alert_id}/triage", response_model=AlertOut)
def update_alert(
    alert_id: int,
    body: TriageRequest,
    db: Session = Depends(get_db),
    viewer: User = Depends(require_roles(*_VIEWERS)),
):
    """Confirm / mark false positive / mark investigated.

    Requires the version currently held by the client; a stale version yields
    409 instead of silently overwriting another analyst's change.
    """
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    ensure_can_view_alert(db, viewer, alert)
    alert = triage_alert(
        db,
        alert=alert,
        actor_id=viewer.id,
        new_status=body.status,
        expected_version=body.version,
        note=body.note,
    )
    db.commit()
    db.refresh(alert)
    return alert
