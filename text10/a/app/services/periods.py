from sqlalchemy import select

from ..errors import DomainError
from ..models import FiscalPeriod, PeriodAuditLog, utcnow


def _lock_period(db, period_id: int) -> FiscalPeriod:
    period = db.scalar(
        select(FiscalPeriod).where(FiscalPeriod.id == period_id).with_for_update()
    )
    if period is None:
        raise DomainError(404, "period-not-found", f"period {period_id} does not exist")
    return period


def close_period(db, period_id: int, actor: str) -> FiscalPeriod:
    """Close a period. Takes the same row lock as posting, so a close and a
    concurrent post take effect in one explicit order: whoever locks first."""
    period = _lock_period(db, period_id)
    if period.status == "closed":
        raise DomainError(409, "period-already-closed", f"period {period_id} is already closed")
    period.status = "closed"
    period.closed_by = actor
    period.closed_at = utcnow()
    db.add(PeriodAuditLog(period_id=period.id, action="close", actor=actor, reason=None))
    db.flush()
    return period


def reopen_period(db, period_id: int, actor: str, reason: str) -> FiscalPeriod:
    """Reopen a closed period. Restricted to finance_manager at the router;
    the reason is mandatory and written to the audit log."""
    period = _lock_period(db, period_id)
    if period.status != "closed":
        raise DomainError(409, "period-not-closed", f"period {period_id} is not closed")
    period.status = "open"
    period.closed_by = None
    period.closed_at = None
    db.add(PeriodAuditLog(period_id=period.id, action="reopen", actor=actor, reason=reason))
    db.flush()
    return period
