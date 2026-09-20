from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import DomainError
from ..models import FiscalPeriod, PeriodAuditLog
from ..schemas import PeriodAuditOut, PeriodCreate, PeriodOut, ReopenRequest
from ..security import CurrentUser, require_role
from ..services.periods import close_period, reopen_period

router = APIRouter(prefix="/periods", tags=["periods"])

Db = Annotated[Session, Depends(get_db)]
Manager = Annotated[CurrentUser, Depends(require_role("finance_manager"))]
Reader = Annotated[CurrentUser, Depends(require_role("clerk", "approver", "finance_manager", "auditor"))]


@router.post("", response_model=PeriodOut, status_code=201)
def create_period(payload: PeriodCreate, db: Db, user: Manager):
    exists = db.scalar(
        select(FiscalPeriod).where(
            FiscalPeriod.year == payload.year, FiscalPeriod.period == payload.period
        )
    )
    if exists is not None:
        raise DomainError(409, "period-exists", f"period already exists with id {exists.id}")
    period = FiscalPeriod(year=payload.year, period=payload.period, status="open")
    db.add(period)
    db.commit()
    return period


@router.get("", response_model=list[PeriodOut])
def list_periods(db: Db, user: Reader, year: int | None = None):
    stmt = select(FiscalPeriod).order_by(FiscalPeriod.year, FiscalPeriod.period)
    if year is not None:
        stmt = stmt.where(FiscalPeriod.year == year)
    return db.scalars(stmt).all()


@router.post("/{period_id}/close", response_model=PeriodOut)
def close_period_endpoint(period_id: int, db: Db, user: Manager):
    period = close_period(db, period_id, user.username)
    db.commit()
    return period


@router.post("/{period_id}/reopen", response_model=PeriodOut)
def reopen_period_endpoint(period_id: int, payload: ReopenRequest, db: Db, user: Manager):
    period = reopen_period(db, period_id, user.username, payload.reason)
    db.commit()
    return period


@router.get("/{period_id}/audits", response_model=list[PeriodAuditOut])
def period_audits(period_id: int, db: Db, user: Reader):
    return db.scalars(
        select(PeriodAuditLog)
        .where(PeriodAuditLog.period_id == period_id)
        .order_by(PeriodAuditLog.id)
    ).all()
