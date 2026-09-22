"""Shared domain helpers: fixed-point money and stable bucketing.

Money
-----
All monetary values flow through :class:`decimal.Decimal` and are persisted in
``DecimalField(max_digits=14, decimal_places=6)`` -- micro-units with 6 decimal
places. Floats are never used for money. The helper accepts strings/Decimals
and rejects NaN/Infinity and values that do not fit the column.

Stable bucketing
----------------
Experiment assignment must be (a) deterministic for a given
(experiment, user_key), (b) 50/50 balanced, and (c) immune to config changes --
the assignment is bound to the experiment id, not to any particular config
version. We use the first 64 bits of SHA-256 and map to [0, 10000); bucket
"A" is the lower half, "B" the upper half.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation, localcontext

from django.conf import settings

ZERO = Decimal("0")
MAX_MONEY = Decimal("99999999.999999")  # Decimal(14, 6) upper bound


def to_money(value, *, field: str = "amount") -> Decimal:
    """Coerce an inbound JSON value to a normalized 6-dp money Decimal.

    JSON numbers arrive as ``float`` through the DRF JSON parser; floats are
    rejected because binary floats cannot represent ad-revenue amounts
    exactly. Clients must send money as a string (e.g. ``"0.123456"``).
    """
    if isinstance(value, bool):  # bool is a subclass of int -- refuse explicitly
        raise ValueError(f"{field} must be a fixed-point decimal string")
    if isinstance(value, float):
        raise ValueError(
            f"{field} must be sent as a string to avoid binary-float rounding "
            f"(got {value!r})"
        )
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} is not a valid decimal: {value!r}") from exc
    if not dec.is_finite():
        raise ValueError(f"{field} must be finite")
    if dec < ZERO:
        raise ValueError(f"{field} must be non-negative")
    if dec > MAX_MONEY:
        raise ValueError(f"{field} exceeds the maximum representable amount")
    return dec.quantize(Decimal(1).scaleb(-settings.MONEY_DECIMAL_PLACES))


def money_field(**kwargs):
    from django.db import models

    defaults = dict(max_digits=14, decimal_places=settings.MONEY_DECIMAL_PLACES, default=ZERO)
    defaults.update(kwargs)
    return models.DecimalField(**defaults)


def weighted_midpoint(numerator: Decimal, denominator: Decimal, places: int = 6) -> Decimal:
    """Rounded half-EVEN division used for eCPM to keep re-runs identical."""
    if denominator == 0:
        return ZERO
    with localcontext() as ctx:
        ctx.prec = 28
        return (numerator / denominator).quantize(
            Decimal(1).scaleb(-places)
        )


def stable_bucket(experiment_key: str, user_key: str) -> str:
    """Deterministic 50/50 assignment for an experiment/user pair.

    Returns "A" or "B". Because the hash input only contains the experiment's
    stable key and the user's stable key, publishing a new config version
    cannot move anyone between groups.
    """
    digest = hashlib.sha256(f"{experiment_key}|{user_key}".encode("utf-8")).digest()
    bucket_int = int.from_bytes(digest[:8], "big") % 10000
    return "A" if bucket_int < 5000 else "B"


def hash_user_key(raw: str) -> str:
    """One-way hash for storing raw SDK user keys (PII minimization)."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
