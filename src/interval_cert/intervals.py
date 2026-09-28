"""Thin, explicit wrapper layer over mpmath's interval arithmetic.

All kernel arithmetic goes through these helpers so outward-rounding
behaviour and the "division by an interval containing zero is a domain
error, not an infinite interval" policy live in exactly one place.

Note: mpmath's ``ivmpf.a`` / ``ivmpf.b`` accessors return zero-width
*intervals*, not scalars. The helpers below convert to genuine ``mpf``
scalars at every boundary so downstream code never has to care.
"""

from __future__ import annotations

from mpmath import iv, mp, mpf

from .errors import DomainEvaluationError

# Re-exported so callers never need to import mpmath types directly.
Interval = type(iv.mpf(0))


def scalar(value) -> mpf:
    """Convert a zero-width interval or number to a genuine mpf scalar."""
    if isinstance(value, Interval):
        return mpf(value)
    return mpf(value)


def make_interval(lo, hi) -> Interval:
    """Build a closed interval [lo, hi] with outward rounding."""
    lo_m, hi_m = scalar(lo), scalar(hi)
    if lo_m > hi_m:
        raise ValueError(f"empty interval: lo={lo_m} > hi={hi_m}")
    return iv.mpf((lo_m, hi_m))


def point_interval(x) -> Interval:
    """Degenerate interval [x, x] (still outward-rounded)."""
    return iv.mpf(scalar(x))


def contains_zero(x: Interval) -> bool:
    return x.a <= 0 <= x.b


def width(x: Interval) -> mpf:
    return scalar(x.b) - scalar(x.a)


def midpoint(x: Interval) -> mpf:
    return (scalar(x.a) + scalar(x.b)) / 2


def is_inside(inner: Interval, outer: Interval) -> bool:
    """True iff ``inner`` is a (non-strict) subset of ``outer``."""
    return inner.a >= outer.a and inner.b <= outer.b


def intersection(a: Interval, b: Interval) -> Interval | None:
    """Intersection of two intervals, or None when disjoint."""
    lo, hi = max(scalar(a.a), scalar(b.a)), min(scalar(a.b), scalar(b.b))
    if lo > hi:
        return None
    return iv.mpf((lo, hi))


def safe_divide(numerator: Interval, denominator: Interval, *, location: str) -> Interval:
    """Interval division that refuses divisors containing zero.

    mpmath silently returns [-inf, +inf] for such divisors, which would
    destroy the rigour of the Newton step, so we fail loudly instead.
    """
    if contains_zero(denominator):
        raise DomainEvaluationError(
            "division by an interval containing zero",
            location=location,
            interval=interval_to_str(denominator),
            reason="divisor_straddles_zero",
        )
    return numerator / denominator


def int_pow(x: Interval, n: int) -> Interval:
    """Non-negative integer power of an interval (tight, outward-rounded)."""
    if n < 0:
        raise ValueError("int_pow requires n >= 0")
    if n == 0:
        return iv.mpf(1)
    return x ** n


def interval_to_str(x: Interval) -> str:
    return f"[{mp.nstr(scalar(x.a), 30)}, {mp.nstr(scalar(x.b), 30)}]"


def interval_to_dict(x: Interval) -> dict[str, str]:
    return {"lo": mp.nstr(scalar(x.a), 30), "hi": mp.nstr(scalar(x.b), 30)}
