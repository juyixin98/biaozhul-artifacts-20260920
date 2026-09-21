"""Maintenance endpoints: controllable clock and seat-expiry sweep.

Supervisor-only. Tests drive deterministic 48h expiry through these
endpoints with the background sweeper disabled.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import clock as clock_svc
from app.database import get_db
from app.deps import CurrentUser, require_supervisor
from app.schemas import AdvanceIn, FreezeIn
from app.services import enrollments as enroll_svc

router = APIRouter(prefix="/admin", tags=["maintenance"])


@router.get("/clock")
def clock_status(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    return clock_svc.status(db)


@router.post("/clock/freeze")
def clock_freeze(
    payload: FreezeIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    current = clock_svc.freeze(db, payload.at)
    db.commit()
    return {"now": current.isoformat(), "frozen": True}


@router.post("/clock/advance")
def clock_advance(
    payload: AdvanceIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    current = clock_svc.advance(db, payload.seconds)
    db.commit()
    return {"now": current.isoformat(), "advanced_seconds": payload.seconds}


@router.post("/clock/reset")
def clock_reset(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    current = clock_svc.reset(db)
    db.commit()
    return {"now": current.isoformat(), "reset": True}


@router.post("/sweep-seat-expiries")
def sweep(
    course_id: int | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    result = enroll_svc.sweep_expired_seats(db, course_id=course_id)
    db.commit()
    return result
