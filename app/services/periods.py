from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import Period, PeriodEvent
from ..schemas import PeriodCloseIn, PeriodIn, PeriodReopenIn


def create_period(db: Session, payload: PeriodIn) -> Period:
    if payload.end_date < payload.start_date:
        raise ValidationError("end_date must be on or after start_date")
    if db.get(Period, payload.code) is not None:
        raise ConflictError(f"Period {payload.code} already exists")
    period = Period(
        code=payload.code,
        start_date=payload.start_date,
        end_date=payload.end_date,
        is_closed=False,
    )
    db.add(period)
    db.flush()
    return period


def close_period(
    db: Session, code: str, payload: PeriodCloseIn, actor: str
) -> Period:
    # Closing takes a row lock so a posting that grabbed the lock first is
    # guaranteed to land before the close flag is set.
    period = db.scalar(select(Period).where(Period.code == code).with_for_update())
    if period is None:
        raise NotFoundError(f"Period {code} does not exist")
    if period.is_closed:
        # Idempotent: closing an already closed period is a no-op replay.
        return period
    period.is_closed = True
    period.closed_reason = payload.reason or None
    period.closed_by = actor
    period.closed_at = func.now()
    db.add(
        PeriodEvent(
            period_code=period.code,
            action="close",
            reason=payload.reason or "(no reason provided)",
            actor=actor,
        )
    )
    db.flush()
    return period


def reopen_period(
    db: Session, code: str, payload: PeriodReopenIn, actor: str
) -> Period:
    period = db.scalar(select(Period).where(Period.code == code).with_for_update())
    if period is None:
        raise NotFoundError(f"Period {code} does not exist")
    if not period.is_closed:
        raise ConflictError(f"Period {code} is already open")
    period.is_closed = False
    period.closed_reason = None
    period.closed_by = None
    period.closed_at = None
    db.add(
        PeriodEvent(
            period_code=period.code,
            action="reopen",
            reason=payload.reason,
            actor=actor,
        )
    )
    db.flush()
    return period


def list_events(db: Session, code: str) -> list[PeriodEvent]:
    if db.get(Period, code) is None:
        raise NotFoundError(f"Period {code} does not exist")
    return list(
        db.scalars(
            select(PeriodEvent)
            .where(PeriodEvent.period_code == code)
            .order_by(PeriodEvent.created_at, PeriodEvent.id)
        )
    )
