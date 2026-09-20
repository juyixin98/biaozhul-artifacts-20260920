"""期间服务：关闭与重开。与过账共用期间行锁，保证明确的生效顺序。"""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Period


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _lock_period(db: Session, period_id: int) -> Period:
    period = db.execute(
        select(Period).where(Period.id == period_id).with_for_update()
    ).scalar_one_or_none()
    if period is None:
        raise _error(404, "PERIOD_NOT_FOUND", f"期间 {period_id} 不存在")
    return period


def close_period(db: Session, *, period_id: int, actor: str) -> Period:
    period = _lock_period(db, period_id)
    if period.status == "closed":
        raise _error(409, "ALREADY_CLOSED", f"期间 {period.year}-{period.month:02d} 已关闭")
    period.status = "closed"
    db.add(AuditLog(actor=actor, action="close_period",
                    entity_type="period", entity_id=str(period.id)))
    db.commit()
    return period


def reopen_period(db: Session, *, period_id: int, actor: str, reason: str) -> Period:
    """仅财务负责人可调用（路由层校验）；必须给出原因并留痕。"""
    period = _lock_period(db, period_id)
    if period.status == "open":
        raise _error(409, "ALREADY_OPEN", f"期间 {period.year}-{period.month:02d} 处于开放状态")
    period.status = "open"
    db.add(AuditLog(actor=actor, action="reopen_period",
                    entity_type="period", entity_id=str(period.id), reason=reason))
    db.commit()
    return period
