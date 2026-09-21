"""Internal endpoints: controllable clock and explicit invitation reaping.

These support deterministic testing and ops; they sit under /internal and are
intended to stay network-local. The clock API can be disabled with
CAREFORCE_ENABLE_CLOCK_API=false in production.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.clock import Clock
from app.config import Settings
from app.db import get_session
from app.deps import get_clock, get_settings_dep
from app.schemas import ClockIn, ClockOut
from app.services.allocation import expire_due_invitations

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/clock", response_model=ClockOut)
def set_clock(
    payload: ClockIn,
    request: Request,
    clock: Clock = Depends(get_clock),
) -> ClockOut:
    if not request.app.state.settings.enable_clock_api:
        from fastapi import HTTPException
        raise HTTPException(404, "clock API disabled")
    if payload.reset:
        clock.reset()
    elif payload.freeze_at is not None:
        clock.freeze(payload.freeze_at)
    elif payload.advance_seconds is not None:
        clock.advance(timedelta(seconds=payload.advance_seconds))
    return ClockOut(now=clock.now())


@router.get("/clock", response_model=ClockOut)
def get_clock_value(clock: Clock = Depends(get_clock)) -> ClockOut:
    return ClockOut(now=clock.now())


@router.post("/reap-expired")
def reap(
    db: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    affected = expire_due_invitations(db, now=clock.now(), settings=settings)
    return {"reallocated_task_ids": affected}
