from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import DomainError
from ..models import Period, PeriodAudit
from ..schemas import CloseRequest, PeriodCreate, ReopenRequest
from ..security import FINANCE_OFFICER, User, get_current_user, require_roles
from ..services import periods as period_service

router = APIRouter(prefix="/periods", tags=["periods"])

OFFICER = require_roles(FINANCE_OFFICER)


@router.post("", status_code=201)
def create_period(
    payload: PeriodCreate,
    db: Session = Depends(get_db),
    user: User = Depends(OFFICER),
):
    return period_service.period_dict(
        period_service.create_period(db, payload.year, payload.month, user.id)
    )


@router.get("")
def list_periods(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    periods = db.scalars(select(Period).order_by(Period.year, Period.month)).all()
    return [period_service.period_dict(p) for p in periods]


@router.post("/{year}/{month}/close")
def close_period(
    year: int,
    month: int,
    payload: CloseRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(OFFICER),
):
    reason = payload.reason if payload else ""
    return period_service.period_dict(
        period_service.close_period(db, year, month, user.id, reason)
    )


@router.post("/{year}/{month}/reopen")
def reopen_period(
    year: int,
    month: int,
    payload: ReopenRequest,
    db: Session = Depends(get_db),
    user: User = Depends(OFFICER),
):
    return period_service.period_dict(
        period_service.reopen_period(db, year, month, payload.reason, user.id)
    )


@router.get("/{year}/{month}/audits")
def period_audits(
    year: int,
    month: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    period = db.scalar(select(Period).where(Period.year == year, Period.month == month))
    if period is None:
        raise DomainError(404, f"period {year}-{month:02d} does not exist")
    audits = db.scalars(
        select(PeriodAudit).where(PeriodAudit.period_id == period.id).order_by(PeriodAudit.id)
    ).all()
    return [period_service.audit_dict(a) for a in audits]
