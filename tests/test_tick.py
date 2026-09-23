"""Tests for the integer tick <-> fixed-point sqrt-price mapping."""

from __future__ import annotations

import random

import pytest

from app.config import SQRT_SCALE, TICK_MAX, TICK_MIN
from app.reference import reference_sqrt_price
from app.tick import (
    format_fixed,
    price_at_tick_decimal,
    sqrt_price_at_tick,
    tick_at_sqrt_price,
    validate_tick_range,
)

BOUNDARY_TICKS = [TICK_MIN, TICK_MIN + 1, -2, -1, 0, 1, 2, TICK_MAX - 1, TICK_MAX]


@pytest.mark.parametrize("tick", BOUNDARY_TICKS)
def test_integer_map_matches_decimal_reference_at_boundaries(tick: int) -> None:
    """floor(10**38 * 1.0001**(tick/2)) via two independent implementations."""
    assert sqrt_price_at_tick(tick) == reference_sqrt_price(tick)


def test_integer_map_matches_decimal_reference_random() -> None:
    rng = random.Random(20260922)
    for _ in range(60):
        tick = rng.randint(TICK_MIN, TICK_MAX)
        assert sqrt_price_at_tick(tick) == reference_sqrt_price(tick), tick


def test_map_strictly_monotone() -> None:
    prev = 0
    for tick in range(-500, 501, 7):
        sp = sqrt_price_at_tick(tick)
        assert sp > prev
        prev = sp


def test_zero_tick_is_exact_scale() -> None:
    assert sqrt_price_at_tick(0) == SQRT_SCALE


def test_price_decimal_relation() -> None:
    # price(tick) = 1.0001 ** tick; the floor-sqrt-price squared is within
    # ~2 sqrt(p)/S of the exact price (S = 10**38 => error below 1e-37).
    from decimal import Decimal, localcontext

    p = price_at_tick_decimal(1000)
    sp = Decimal(sqrt_price_at_tick(1000))
    s = Decimal(SQRT_SCALE)
    with localcontext() as ctx:
        ctx.prec = 90
        ratio = sp ** 2 / s ** 2
        assert abs(ratio - p) < Decimal("1e-37")


def test_tick_inversion_roundtrip() -> None:
    for tick in [-12345, -7, -1, 0, 1, 7, 12345]:
        sp = sqrt_price_at_tick(tick)
        assert tick_at_sqrt_price(sp) == tick
        # One unit below the canonical price belongs to the previous tick.
        if tick > TICK_MIN:
            assert tick_at_sqrt_price(sp - 1) == tick - 1


def test_tick_inversion_on_boundary_prices() -> None:
    for tick in BOUNDARY_TICKS:
        assert tick_at_sqrt_price(sqrt_price_at_tick(tick)) == tick


def test_validate_tick_range() -> None:
    validate_tick_range(TICK_MIN)
    validate_tick_range(TICK_MAX)
    with pytest.raises(ValueError):
        validate_tick_range(TICK_MIN - 1)
    with pytest.raises(ValueError):
        validate_tick_range(TICK_MAX + 1)
    with pytest.raises(TypeError):
        validate_tick_range(1.5)  # type: ignore[arg-type]


def test_format_fixed() -> None:
    assert format_fixed(SQRT_SCALE) == "1." + "0" * 38
