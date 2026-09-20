"""Controllable clock.

All scheduling logic reads "now" from :class:`Clock` rather than calling
``datetime.now()`` directly. Production uses :class:`SystemClock`; tests inject
:class:`FakeClock` to deterministically exercise invitation expiry,
synchronisation sweeps and task generation boundaries.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock


class Clock:
    def now(self) -> datetime:
        """Return current time as a timezone-aware UTC datetime."""
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock(Clock):
    """A clock that only moves when a test advances it."""

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            start = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)  # a Monday
        if start.tzinfo is None:
            raise ValueError("FakeClock start must be timezone-aware")
        self._now = start.astimezone(timezone.utc)
        self._lock = Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, **kwargs: float) -> datetime:
        with self._lock:
            self._now = self._now + timedelta(**kwargs)
            return self._now

    def set(self, value: datetime) -> datetime:
        with self._lock:
            if value.tzinfo is None:
                raise ValueError("value must be timezone-aware")
            self._now = value.astimezone(timezone.utc)
            return self._now
