"""Controllable application clock.

Every time-sensitive business rule (enrolment deadline, 48h seat
confirmation window) reads *this* clock instead of ``datetime.now``.

In normal operation the stored offset is zero and ``now()`` returns the
real wall-clock time. Tests (and the maintenance API) freeze or advance
the stored offset, which makes expiry logic deterministic.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AppClock

_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _row(db: Session) -> AppClock:
    row = db.scalar(select(AppClock).where(AppClock.id == 1))
    if row is None:
        # Created by the initial migration; create defensively if missing.
        row = AppClock(id=1, offset_seconds=0.0)
        db.add(row)
        db.flush()
    return row


def now(db: Session) -> datetime:
    """Current application time (timezone-aware UTC)."""
    # Read real time from the DB server perspective; Python host clock is fine
    # because all components share this abstraction and tests control offset.
    return datetime.now(timezone.utc) + timedelta(seconds=_row(db).offset_seconds)


def freeze(db: Session, frozen_at: datetime) -> datetime:
    """Freeze the clock at ``frozen_at``."""
    if frozen_at.tzinfo is None:
        frozen_at = frozen_at.replace(tzinfo=timezone.utc)
    real = datetime.now(timezone.utc)
    row = _row(db)
    row.offset_seconds = (frozen_at - real).total_seconds()
    db.flush()
    return now(db)


def advance(db: Session, seconds: float) -> datetime:
    """Advance the (possibly frozen) clock by a number of seconds."""
    row = _row(db)
    row.offset_seconds += seconds
    db.flush()
    return now(db)


def reset(db: Session) -> datetime:
    """Return the clock to real wall-clock time."""
    row = _row(db)
    row.offset_seconds = 0.0
    db.flush()
    return now(db)


def status(db: Session) -> dict:
    current = now(db)
    return {
        "now": current.isoformat(),
        "offset_seconds": _row(db).offset_seconds,
        "real_now": datetime.now(timezone.utc).isoformat(),
        "reference_epoch": _EPOCH.isoformat(),
    }
