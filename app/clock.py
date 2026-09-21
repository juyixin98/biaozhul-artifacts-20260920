"""Controllable clock.

Everything that depends on "now" reads it through :class:`Clock` instead of
``datetime.now`` directly, which makes expiry / rescheduling tests deterministic.

Production code gets the clock from FastAPI ``app.state.clock``; tests may
``freeze`` / ``advance`` / ``reset`` it (or hit ``/internal/clock`` when the
clock API is enabled).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock


class Clock:
    def __init__(self, frozen_at: datetime | None = None) -> None:
        self._lock = Lock()
        self._frozen_at: datetime | None = self._as_utc(frozen_at) if frozen_at else None
        self._offset: timedelta = timedelta(0)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def now(self) -> datetime:
        """Current time as a timezone-aware UTC datetime."""
        with self._lock:
            if self._frozen_at is not None:
                return self._frozen_at + self._offset
            return datetime.now(timezone.utc) + self._offset

    def freeze(self, value: datetime) -> None:
        with self._lock:
            self._frozen_at = self._as_utc(value)
            self._offset = timedelta(0)

    def advance(self, delta: timedelta) -> datetime:
        with self._lock:
            if self._frozen_at is None:
                # Anchor at the real current time the first time we advance.
                self._frozen_at = datetime.now(timezone.utc)
            self._offset += delta
            return self._frozen_at + self._offset

    def reset(self) -> None:
        with self._lock:
            self._frozen_at = None
            self._offset = timedelta(0)
