from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..schemas import (
    PeriodCloseIn,
    PeriodIn,
    PeriodOut,
    PeriodReopenIn,
)
from ..security import Principal, require_roles
from ..services.periods import close_period, create_period, list_events, reopen_period

router = APIRouter(tags=["periods"])


def _period_out(p) -> dict:
    return {
        "code": p.code,
        "start_date": p.start_date,
        "end_date": p.end_date,
        "is_closed": p.is_closed,
        "closed_reason": p.closed_reason,
        "closed_at": p.closed_at,
        "closed_by": p.closed_by,
    }


@router.post("/periods", response_model=PeriodOut, status_code=201)
def post_period(
    payload: PeriodIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_roles("lead")),
):
    period = create_period(db, payload)
    db.commit()
    return _period_out(period)


@router.get("/periods", response_model=list[PeriodOut])
def get_periods(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_roles("lead", "accountant", "auditor")),
):
    from sqlalchemy import select

    from ..models import Period

    return [
        _period_out(p) for p in db.scalars(select(Period).order_by(Period.code))
    ]


@router.post("/periods/{code}/close", response_model=PeriodOut)
def post_close(
    code: str,
    payload: PeriodCloseIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_roles("lead")),
):
    period = close_period(db, code, payload, actor=principal.name)
    db.commit()
    return _period_out(period)


@router.post("/periods/{code}/reopen", response_model=PeriodOut)
def post_reopen(
    code: str,
    payload: PeriodReopenIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_roles("lead")),
):
    period = reopen_period(db, code, payload, actor=principal.name)
    db.commit()
    return _period_out(period)


@router.get("/periods/{code}/events")
def get_events(
    code: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_roles("lead", "accountant", "auditor")),
):
    events = list_events(db, code)
    return [
        {
            "id": e.id,
            "period_code": e.period_code,
            "action": e.action,
            "reason": e.reason,
            "actor": e.actor,
            "created_at": e.created_at,
        }
        for e in events
    ]
