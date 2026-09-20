from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Period
from app.schemas import PeriodCreateIn, PeriodOut, ReopenIn
from app.security import Identity, get_identity, require_manager, require_write
from app.services import periods as period_service

router = APIRouter(prefix="/periods", tags=["periods"])


@router.post("", response_model=PeriodOut, status_code=201)
def create_period(
    body: PeriodCreateIn,
    db: Session = Depends(get_db),
    _: Identity = Depends(require_write),
):
    period = Period(year=body.year, month=body.month, status="open")
    db.add(period)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_PERIOD",
                                                     "message": f"期间 {body.year}-{body.month:02d} 已存在"})
    return PeriodOut.model_validate(period)


@router.get("", response_model=list[PeriodOut])
def list_periods(
    db: Session = Depends(get_db),
    _: Identity = Depends(get_identity),
):
    stmt = select(Period).order_by(Period.year, Period.month)
    return [PeriodOut.model_validate(p) for p in db.scalars(stmt)]


@router.post("/{period_id}/close", response_model=PeriodOut)
def close_period(
    period_id: int,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_write),
):
    period = period_service.close_period(db, period_id=period_id, actor=identity.name)
    return PeriodOut.model_validate(period)


@router.post("/{period_id}/reopen", response_model=PeriodOut)
def reopen_period(
    period_id: int,
    body: ReopenIn,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_manager),
):
    """仅财务负责人可重开，必须说明原因（写入审计日志）。"""
    period = period_service.reopen_period(
        db, period_id=period_id, actor=identity.name, reason=body.reason
    )
    return PeriodOut.model_validate(period)
