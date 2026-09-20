from datetime import timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import clock
from ..config import get_settings
from ..database import get_db
from ..deps import current_manager
from ..errors import forbidden
from ..models import ClockOverride, User
from ..schemas import ClockAdvance, ClockSet

router = APIRouter(prefix="/admin/clock", tags=["clock"])


def _ensure_controlled() -> None:
    if not get_settings().allow_clock_control:
        raise forbidden("clock control is disabled")


@router.get("", response_model=dict)
def read_clock(db: Session = Depends(get_db)) -> dict:
    override = db.get(ClockOverride, "global")
    return {
        "now": clock.utcnow(db),
        "virtual": override is not None and override.virtual_now is not None,
    }


@router.put("", response_model=dict)
def set_clock(
    payload: ClockSet,
    db: Session = Depends(get_db),
    manager: User = Depends(current_manager),
) -> dict:
    _ensure_controlled()
    value = payload.now
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    now = clock.set_clock(db, value)
    db.commit()
    return {"now": now}


@router.post("/advance", response_model=dict)
def advance(
    payload: ClockAdvance,
    db: Session = Depends(get_db),
    manager: User = Depends(current_manager),
) -> dict:
    _ensure_controlled()
    now = clock.advance(db, payload.delta())
    db.commit()
    return {"now": now}


@router.post("/reset", response_model=dict)
def reset(
    db: Session = Depends(get_db),
    manager: User = Depends(current_manager),
) -> dict:
    _ensure_controlled()
    now = clock.reset(db)
    db.commit()
    return {"now": now}
