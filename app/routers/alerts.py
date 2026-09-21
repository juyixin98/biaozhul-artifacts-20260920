from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, get_visible_alert_or_403, scoped_alerts_query
from app.models import Alert, Employee, User
from app.schemas import AlertOut, AlertPatch
from app.services.triage import VersionConflictError, update_alert_status

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def _to_out(alert: Alert) -> AlertOut:
    return AlertOut(
        id=alert.id,
        employee_id=alert.employee_id,
        employee_name=alert.employee.name,
        department_id=alert.employee.department_id,
        rule=alert.rule,
        window_start=alert.window_start,
        window_end=alert.window_end,
        status=alert.status,
        version=alert.version,
        evidence=alert.evidence,
        created_at=alert.created_at,
        updated_at=alert.updated_at,
    )


@router.get("", response_model=list[AlertOut])
def list_alerts(
    status: str | None = Query(default=None),
    rule: str | None = Query(default=None),
    employee_id: int | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = scoped_alerts_query(db, user)
    if status:
        q = q.filter(Alert.status == status)
    if rule:
        q = q.filter(Alert.rule == rule)
    if employee_id is not None:
        q = q.filter(Alert.employee_id == employee_id)
    alerts = q.order_by(Alert.id).limit(limit).offset(offset).all()
    return [_to_out(a) for a in alerts]


@router.get("/{alert_id}", response_model=AlertOut)
def get_alert(
    alert_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    alert = get_visible_alert_or_403(db, user, alert_id)
    return _to_out(alert)


@router.patch("/{alert_id}", response_model=AlertOut)
def patch_alert(
    alert_id: int,
    body: AlertPatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    get_visible_alert_or_403(db, user, alert_id)
    try:
        alert = update_alert_status(db, alert_id, body.status, body.version)
        db.commit()
    except VersionConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    db.refresh(alert)
    return _to_out(alert)
