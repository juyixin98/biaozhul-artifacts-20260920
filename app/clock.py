"""Controllable clock.

All business logic reads "now" through ``clock.now(utc=True)``. In production
this returns the real wall clock; tests replace ``clock.now_fn`` (see the
``frozen_clock`` pytest fixture) so invitation expiry, weekly boundaries and
regeneration horizons are all deterministic.
"""

from datetime import datetime, timezone


def _system_now() -> datetime:
    return datetime.now(timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now_fn = _system_now

    def now(self) -> datetime:
        """Current time as a timezone-aware UTC datetime."""
        value = self.now_fn()
        if value.tzinfo is None:
            raise ValueError("clock.now_fn must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("fixed clock time must be timezone-aware")
        self.now_fn = lambda: value.astimezone(timezone.utc)

    def reset(self) -> None:
        self.now_fn = _system_now


clock = Clock()
