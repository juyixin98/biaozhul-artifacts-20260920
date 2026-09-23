"""Decimal context helpers and integer <-> normalized amount conversion."""

from __future__ import annotations

import decimal
from contextlib import contextmanager
from typing import Iterator

from . import config


@contextmanager
def high_precision() -> Iterator[decimal.Context]:
    """Run a block under a local high-precision Decimal context.

    The Decimal *default* context is global; mutating it would leak across
    requests.  ``localcontext`` makes the precision change scoped.  We also
    pin trap flags so that NaN/Inf (e.g. division by zero) raises instead of
    silently propagating.
    """
    # The Decimal Context constructor requires *all* signal keys in its
    # traps/flags dicts (a partial dict raises "invalid signal dict"), so we
    # start from the current context's signal set. Trap only genuinely
    # invalid conditions; do NOT trap Inexact/Rounded, because at fixed
    # 80-digit precision ordinary division rounds and would otherwise raise
    # on nearly every operation.
    base = decimal.getcontext()
    traps = {sig: False for sig in base.traps}
    for sig in (decimal.DivisionByZero, decimal.InvalidOperation, decimal.Overflow):
        traps[sig] = True
    ctx = decimal.Context(
        prec=config.DECIMAL_PREC,
        rounding=config.INTERNAL_ROUNDING,
        traps=traps,
        flags={sig: 0 for sig in base.flags},
        Emax=999999,
        Emin=-999999,
    )
    with decimal.localcontext(ctx):
        yield ctx


def d(value: int | str | decimal.Decimal) -> decimal.Decimal:
    """Exact Decimal construction (never via float)."""
    return decimal.Decimal(value)


def scale_factor(decimals: int) -> decimal.Decimal:
    return d(10) ** decimals


def to_normalized(amount_units: int, decimals: int) -> decimal.Decimal:
    """Integer token units -> normalized (human-scaled) Decimal amount."""
    if amount_units < 0:
        raise ValueError("amount must be non-negative")
    return d(amount_units) / scale_factor(decimals)


def to_units_floor(amount_normalized: decimal.Decimal, decimals: int) -> int:
    """Normalized Decimal -> integer token units, rounded toward zero."""
    if amount_normalized < 0:
        raise ValueError("amount must be non-negative")
    q = amount_normalized * scale_factor(decimals)
    return int(q.to_integral_value(rounding=config.OUTPUT_ROUNDING))


def relative_error(a: decimal.Decimal, b: decimal.Decimal) -> decimal.Decimal:
    """|a - b| / max(|a|, |b|, 1); Decimal-safe, never divides by zero."""
    denom = max(abs(a), abs(b), d(1))
    return abs(a - b) / denom
