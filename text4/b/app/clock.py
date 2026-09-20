"""Controllable clock.

All business logic reads "now" through :func:`now_utc`.  Tests pin the clock
with :func:`set_clock` / :func:`reset_clock`, so 48-hour seat-expiry behaviour
can be exercised deterministically without sleeping.

The override is a process-global (not a ``ContextVar``) on purpose: the API is
exercised through Starlette's ``TestClient``, which runs the application in a
worker thread where a context-local value would not be visible.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

_override: Optional[datetime] = None


def now_utc() -> datetime:
    if _override is not None:
        return _override
    return datetime.now(timezone.utc)


def set_clock(value: datetime) -> None:
    global _override
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    _override = value


def reset_clock() -> None:
    global _override
    _override = None
