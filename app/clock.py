"""Controllable clock.

All business logic reads the current time through ``clock.now()`` so tests
(and demos) can substitute a fake clock to verify time-dependent behaviour
such as the 48h seat-hold expiry.
"""
from datetime import datetime, timezone


def _real_now() -> datetime:
    return datetime.now(timezone.utc)


_now_fn = _real_now


def now() -> datetime:
    return _now_fn()


def set_clock(fn) -> None:
    global _now_fn
    _now_fn = fn


def reset_clock() -> None:
    global _now_fn
    _now_fn = _real_now
