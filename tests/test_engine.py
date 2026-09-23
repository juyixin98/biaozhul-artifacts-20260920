"""Core engine tests: piecewise swap, fees, stops, and Decimal/Fraction cross-check."""

from __future__ import annotations

import random

import pytest

from app.crypto import digest_pool
from app.engine import gross_for_curve, quote_swap
from app.models import Pool, Position
from app.reference import verify_against_engine

F_NUM, F_DEN = 3_000, 1_000_000


def make_pool(tick: int, positions, tid: str = "t") -> Pool:
    return Pool(
        pool_id=tid, token0="USDC", token1="WETH",
        fee_numerator=F_NUM, fee_denominator=F_DEN,
        current_tick=tick, positions=tuple(positions),
    )


def assert_verified(pool: Pool, result) -> None:
    errors = verify_against_engine(pool, result)
    assert errors == [], errors


# ---------------------------------------------------------------------------
# Basic single-interval behavior, both directions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("zero_for_one", [True, False])
def test_single_interval_partial_fill(zero_for_one: bool) -> None:
    pool = make_pool(1000, [Position(900, 1100, 10**30)])
    r = quote_swap(pool, zero_for_one=zero_for_one, amount_in=10**18)
    assert_verified(pool, r)
    assert r.stop_reason == "input_exhausted"
    assert int(r.unspent_input) == 0
    assert int(r.amount_out) > 0
    assert len([s for s in r.segments if int(s.gross_input) > 0]) == 1
    # Price moved in the correct direction.
    before, after = int(r.sqrt_price_before), int(r.sqrt_price_after)
    assert after < before if zero_for_one else after > before


def test_fee_matches_ceil_identity() -> None:
    pool = make_pool(1000, [Position(900, 1100, 10**30)])
    r = quote_swap(pool, zero_for_one=True, amount_in=123456789)
    for seg in r.segments:
        curve = int(seg.curve_input)
        fee = int(seg.fee_input)
        gross = int(seg.gross_input)
        assert gross == curve + fee
        assert gross == gross_for_curve(curve, F_NUM, F_DEN)
        if curve > 0:
            assert fee >= 1  # cost is never skipped
    assert int(r.fee_paid) == sum(int(s.fee_input) for s in r.segments)


# ---------------------------------------------------------------------------
# Multi-interval crossing (forward and reverse)
# ---------------------------------------------------------------------------

def test_cross_multiple_intervals_up_then_stop_at_gap() -> None:
    pool = make_pool(
        50,
        [Position(0, 100, 10**32), Position(100, 200, 10**30),
         Position(200, 300, 10**28)],
    )
    r = quote_swap(pool, zero_for_one=False, amount_in=10**40)
    assert_verified(pool, r)
    crossed = [s for s in r.segments if s.crossed]
    assert len(crossed) >= 2  # walked through tick 100 and 200
    # Above tick 300 there is no liquidity: the swap must stop there.
    assert r.stop_reason == "empty_range"
    assert int(r.unspent_input) > 0
    assert r.current_tick_after == 300
    # Evidence chain continuity: next start == previous end.
    traded = [s for s in r.segments if int(s.gross_input) > 0]
    for a, b in zip(traded, traded[1:]):
        assert a.end_sqrt_price == b.start_sqrt_price


def test_cross_multiple_intervals_down() -> None:
    pool = make_pool(
        250,
        [Position(0, 100, 10**32), Position(100, 200, 10**30),
         Position(200, 300, 10**28)],
    )
    r = quote_swap(pool, zero_for_one=True, amount_in=10**40)
    assert_verified(pool, r)
    assert len([s for s in r.segments if s.crossed]) >= 2
    assert r.stop_reason == "empty_range"
    assert int(r.unspent_input) > 0
    assert r.current_tick_after == 0


def test_empty_interval_returns_entire_unspent_input() -> None:
    # No positions at all: every interval is empty, nothing can trade.
    pool = make_pool(0, [], "empty")
    for zfo in (True, False):
        r = quote_swap(pool, zero_for_one=zfo, amount_in=10**24)
        assert r.stop_reason == "empty_range"
        assert int(r.amount_out) == 0
        assert int(r.fee_paid) == 0
        assert int(r.unspent_input) == 10**24
        assert_verified(pool, r)


def test_gap_after_some_filled_segments() -> None:
    # Fills in [100,200), gap over [200,300), more liquidity above.
    pool = make_pool(
        150,
        [Position(100, 200, 10**30), Position(300, 400, 10**30)],
    )
    r = quote_swap(pool, zero_for_one=False, amount_in=10**40)
    assert_verified(pool, r)
    assert r.stop_reason == "empty_range"
    assert r.current_tick_after == 200
    assert int(r.amount_out) > 0  # something traded before the gap
    assert int(r.unspent_input) > 0  # remainder returned, not lost


# ---------------------------------------------------------------------------
# Boundary ticks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("start_tick", [100, 200])
def test_start_exactly_on_position_boundary_up(start_tick: int) -> None:
    pool = make_pool(start_tick, [Position(100, 200, 10**30)])
    r = quote_swap(pool, zero_for_one=False, amount_in=10**18)
    assert_verified(pool, r)
    # No liquidity above the upper edge; trading happens only if start < 200.
    if start_tick == 200:
        assert r.stop_reason == "empty_range"
        assert int(r.amount_out) == 0
    else:
        assert int(r.amount_out) > 0


@pytest.mark.parametrize("start_tick", [100, 200])
def test_start_exactly_on_position_boundary_down(start_tick: int) -> None:
    pool = make_pool(start_tick, [Position(100, 200, 10**30)])
    r = quote_swap(pool, zero_for_one=True, amount_in=10**18)
    assert_verified(pool, r)
    if start_tick == 100:
        assert r.stop_reason == "empty_range"
        assert int(r.amount_out) == 0
    else:
        assert int(r.amount_out) > 0


# ---------------------------------------------------------------------------
# Tiny liquidity / dust / maximum input
# ---------------------------------------------------------------------------

def test_tiny_liquidity_dust_stops() -> None:
    pool = make_pool(0, [Position(-10, 10, 1)])  # liquidity of 1 unit
    r = quote_swap(pool, zero_for_one=True, amount_in=1)
    assert_verified(pool, r)
    assert r.stop_reason in ("input_dust", "empty_range")
    assert int(r.amount_out) == 0
    assert int(r.unspent_input) == 1


def test_tiny_liquidity_large_input_crosses() -> None:
    pool = make_pool(0, [Position(-10, 10, 1)])
    r = quote_swap(pool, zero_for_one=False, amount_in=10**20)
    assert_verified(pool, r)
    # With L=1 the interval may be crossed; either way conservation must hold.
    assert int(r.amount_out) >= 0
    total_gross = sum(int(s.gross_input) for s in r.segments)
    assert total_gross + int(r.unspent_input) == 10**20


@pytest.mark.parametrize("zero_for_one", [True, False])
def test_maximum_input_terminates(zero_for_one: bool) -> None:
    pool = make_pool(
        0, [Position(-99999, 99999, 10**42)],
    )
    r = quote_swap(pool, zero_for_one=zero_for_one, amount_in=10**80)
    assert_verified(pool, r)
    assert r.stop_reason in ("empty_range", "tick_boundary")
    total_gross = sum(int(s.gross_input) for s in r.segments)
    assert total_gross + int(r.unspent_input) == 10**80
    assert int(r.amount_out) > 0


def test_dust_input_returns_full_amount() -> None:
    pool = make_pool(1000, [Position(900, 1100, 10**30)])
    r = quote_swap(pool, zero_for_one=True, amount_in=1)
    assert int(r.amount_out) == 0
    assert int(r.unspent_input) == 1
    assert_verified(pool, r)


# ---------------------------------------------------------------------------
# Snapshot immutability
# ---------------------------------------------------------------------------

def test_quote_does_not_modify_pool_snapshot() -> None:
    pool = make_pool(1000, [Position(900, 1100, 10**30)])
    before = pool.canonical()
    digest_before = digest_pool(pool)
    sp_before = pool.sqrt_price
    quote_swap(pool, zero_for_one=True, amount_in=10**40)
    quote_swap(pool, zero_for_one=False, amount_in=10**40)
    assert pool.canonical() == before
    assert digest_pool(pool) == digest_before
    assert pool.sqrt_price == sp_before
    assert pool.current_tick == 1000


def test_result_is_bound_to_snapshot_digest() -> None:
    pool = make_pool(1000, [Position(900, 1100, 10**30)])
    r = quote_swap(pool, zero_for_one=True, amount_in=10**18)
    assert r.pool_digest == digest_pool(pool)
    assert len(r.pool_digest) == 64 and len(r.quote_digest) == 64
    # Different position set -> different digest.
    other = make_pool(1000, [Position(900, 1100, 10**30 + 1)])
    assert digest_pool(other) != r.pool_digest


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [0, -1])
def test_rejects_non_positive_input(bad: int) -> None:
    pool = make_pool(0, [Position(-10, 10, 10**10)])
    with pytest.raises(ValueError):
        quote_swap(pool, zero_for_one=True, amount_in=bad)


# ---------------------------------------------------------------------------
# Randomized differential testing vs the Fraction reference
# ---------------------------------------------------------------------------

def test_randomized_matches_reference() -> None:
    rng = random.Random(424242)
    for case in range(180):
        n_pos = rng.randint(0, 5)
        positions = []
        for _ in range(n_pos):
            a = rng.randint(-500, 500)
            width = rng.randint(5, 400)
            lo, hi = sorted((a, a + width))
            hi = min(hi, 100_000)
            lo = max(lo, -100_000)
            if lo >= hi:
                continue
            liq = rng.choice([1, 2, 10**3, 10**12, 10**30, 10**50])
            positions.append(Position(lo, hi, liq))
        tick = rng.randint(-600, 600)
        pool = make_pool(tick, positions, f"fuzz{case}")
        zfo = rng.choice([True, False])
        amount = 10 ** rng.randint(0, 50) + rng.randint(0, 10**6)
        r = quote_swap(pool, zero_for_one=zfo, amount_in=amount)
        assert_verified(pool, r)
        # Every reported number must be a canonical non-negative integer string.
        for seg in r.segments:
            for key in ("gross_input", "curve_input", "fee_input", "output", "liquidity"):
                v = getattr(seg, key)
                assert int(v) >= 0
