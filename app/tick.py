"""Integer-tick <-> fixed-point sqrt-price mapping.

Exact mapping (no floating point in the hot path)::

    SP(i) = floor(SQRT_SCALE * sqrt((BASE_NUM / BASE_DEN) ** i))

implemented as

    SP(i) = isqrt( (SQRT_SCALE**2 * num) // den )

with ``num/den = (BASE_NUM/BASE_DEN)**i`` (flipped for negative ``i``).

Identity used (valid for all positive real ``x``)::

    isqrt(floor(x)) == floor(sqrt(x))

so the integer result is *exactly* the mathematical floor, never an
off-by-one approximation.

The tick base is ``1.0001`` (Uniswap V3 compatible), so the human-readable
float price is ``1.0001 ** tick`` and the human-readable sqrt price is
``1.0001 ** (tick / 2)``.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from functools import lru_cache
from math import isqrt

from .config import BASE_DEN, BASE_NUM, SQRT_DECIMALS, SQRT_SCALE, TICK_MAX, TICK_MIN

__all__ = [
    "sqrt_price_at_tick",
    "price_at_tick_decimal",
    "sqrt_price_at_tick_decimal",
    "tick_at_sqrt_price",
    "validate_tick_range",
]


def validate_tick_range(tick: int) -> None:
    """Reject ticks outside the project universe."""
    if not isinstance(tick, int):
        raise TypeError("tick must be an integer")
    if tick < TICK_MIN or tick > TICK_MAX:
        raise ValueError(f"tick {tick} out of range [{TICK_MIN}, {TICK_MAX}]")


@lru_cache(maxsize=4096)
def sqrt_price_at_tick(tick: int) -> int:
    """Return the canonical fixed-point sqrt price ``SP(tick)`` (exact floor)."""
    validate_tick_range(tick)
    if tick >= 0:
        num = pow(BASE_NUM, tick)
        den = pow(BASE_DEN, tick)
    else:
        e = -tick
        num = pow(BASE_DEN, e)
        den = pow(BASE_NUM, e)
    # isqrt(floor(S^2 * num / den)) == floor(S * sqrt(num/den))
    return isqrt((SQRT_SCALE * SQRT_SCALE * num) // den)


def sqrt_price_at_tick_decimal(tick: int, digits: int = 80) -> Decimal:
    """High-precision *independent* Decimal sqrt price, scaled by SQRT_SCALE.

    Used by the slow reference path and for human-readable evidence; never by
    the hot engine.  Computed as ``10**38 * 1.0001**(tick/2)`` with Decimal
    exponentiation, so it is an independent implementation of the same map.
    """
    validate_tick_range(tick)
    with localcontext() as ctx:
        ctx.prec = digits
        base = Decimal(BASE_NUM) / Decimal(BASE_DEN)
        return Decimal(SQRT_SCALE) * (base ** (Decimal(tick) / 2))


def price_at_tick_decimal(tick: int, digits: int = 80) -> Decimal:
    """Human-readable float price ``1.0001 ** tick`` as a high-precision Decimal."""
    validate_tick_range(tick)
    with localcontext() as ctx:
        ctx.prec = digits
        base = Decimal(BASE_NUM) / Decimal(BASE_DEN)
        return base ** Decimal(tick)


def tick_at_sqrt_price(sp: int) -> int:
    """Return the largest tick ``i`` with ``SP(i) <= sp`` (floor tick of a price).

    A fast Decimal-log estimate is corrected with exact integer comparisons,
    so the result is exact even at tick boundaries.
    """
    if sp <= 0:
        raise ValueError("sqrt price must be positive")

    # --- estimate with high-precision Decimal logs ------------------------
    with localcontext() as ctx:
        ctx.prec = 80
        ln_base = (Decimal(BASE_NUM) / Decimal(BASE_DEN)).ln()
        ln_sp = (Decimal(sp) / Decimal(SQRT_SCALE)).ln()
        # sp/S = sqrt(p)  =>  tick = floor(2 * ln(sp/S) / ln(base))
        candidate = int((2 * ln_sp / ln_base).to_integral_value(rounding="ROUND_FLOOR"))

    # Keep the estimate inside the universe, then correct exactly.
    candidate = max(TICK_MIN - 2, min(TICK_MAX + 2, candidate))
    while candidate < TICK_MAX and sqrt_price_at_tick(candidate + 1) <= sp:
        candidate += 1
    while candidate > TICK_MIN and sqrt_price_at_tick(candidate) > sp:
        candidate -= 1
    if candidate < TICK_MIN or candidate > TICK_MAX:
        raise ValueError(f"sqrt price {sp} maps outside tick universe")
    return candidate


def format_fixed(sp: int, digits: int = SQRT_DECIMALS) -> str:
    """Render a fixed-point sqrt price integer as a decimal string."""
    whole, frac = divmod(sp, SQRT_SCALE)
    return f"{whole}.{frac:0{digits}d}"
