"""Controllable clock.

Production code never reads ``datetime.now`` directly.  ``now`` is advanced
either naturally (no override row) or deterministically through the
``/admin/clock`` API, which makes the 48h seat-expiry logic testable without
real sleeps.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ClockOverride

_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


def utcnow(db: Session) -> datetime:
    row = db.get(ClockOverride, "global")
    if row is not None and row.virtual_now is not None:
        return row.virtual_now
    return datetime.now(timezone.utc)


def set_clock(db: Session, value: datetime) -> datetime:
    value = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    row = db.get(ClockOverride, "global")
    if row is None:
        row = ClockOverride(id="global", virtual_now=value)
        db.add(row)
    else:
        row.virtual_now = value
    db.flush()
    return value


def advance(db: Session, delta: timedelta) -> datetime:
    return set_clock(db, utcnow(db) + delta)


def reset(db: Session) -> datetime:
    row = db.get(ClockOverride, "global")
    value = datetime.now(timezone.utc)
    if row is not None:
        row.virtual_now = None
    db.flush()
    return value


def stable_now() -> datetime:
    """A fixed timestamp used for demo seeds so output stays deterministic."""
    return _EPOCH
