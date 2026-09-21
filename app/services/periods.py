"""Period lifecycle: create, close, reopen (with audit trail)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..errors import DomainError
from ..models import Period, PeriodAudit


def create_period(db: Session, year: int, month: int, actor: str) -> Period:
    period = Period(year=year, month=month)
    db.add(period)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DomainError(409, f"period {year}-{month:02d} already exists")
    db.refresh(period)
    return period


def _get_period(db: Session, year: int, month: int) -> Period:
    period = db.scalar(select(Period).where(Period.year == year, Period.month == month))
    if period is None:
        raise DomainError(404, f"period {year}-{month:02d} does not exist")
    return period


def close_period(db: Session, year: int, month: int, actor: str, reason: str = "") -> Period:
    """Close an open period. The guarded UPDATE takes the same row lock that
    posting takes, so close and post take effect in one explicit order."""
    period = _get_period(db, year, month)
    res = db.execute(
        update(Period)
        .where(Period.id == period.id, Period.status == "open")
        .values(
            status="closed",
            closed_by=actor,
            closed_at=datetime.now(timezone.utc),
            lock_version=Period.lock_version + 1,
        )
    )
    if res.rowcount != 1:
        raise DomainError(409, f"period {year}-{month:02d} is already closed")
    db.add(PeriodAudit(period_id=period.id, action="close", reason=reason, actor=actor))
    db.commit()
    db.refresh(period)
    return period


def reopen_period(db: Session, year: int, month: int, reason: str, actor: str) -> Period:
    """Reopen a closed period. Caller must be a finance officer (enforced in
    the router) and a reason is mandatory; both are written to the audit log."""
    if not reason.strip():
        raise DomainError(422, "a reason is required to reopen a period")
    period = _get_period(db, year, month)
    res = db.execute(
        update(Period)
        .where(Period.id == period.id, Period.status == "closed")
        .values(status="open", lock_version=Period.lock_version + 1)
    )
    if res.rowcount != 1:
        raise DomainError(409, f"period {year}-{month:02d} is not closed")
    db.add(PeriodAudit(period_id=period.id, action="reopen", reason=reason, actor=actor))
    db.commit()
    db.refresh(period)
    return period


def period_dict(period: Period) -> dict:
    return {
        "id": period.id,
        "year": period.year,
        "month": period.month,
        "status": period.status,
        "closed_by": period.closed_by,
        "closed_at": period.closed_at.isoformat() if period.closed_at else None,
    }


def audit_dict(audit: PeriodAudit) -> dict:
    return {
        "id": audit.id,
        "period_id": audit.period_id,
        "action": audit.action,
        "reason": audit.reason,
        "actor": audit.actor,
        "created_at": audit.created_at.isoformat() if audit.created_at else None,
    }
