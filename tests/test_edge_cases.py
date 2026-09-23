"""Aggressive differential tests: wide tick ranges, gaps, edges, huge inputs."""

from __future__ import annotations

import random

from app.config import TICK_MAX, TICK_MIN
from app.engine import quote_swap
from app.models import Pool, Position
from app.reference import reference_sqrt_price, verify_against_engine
from app.tick import sqrt_price_at_tick

F_NUM, F_DEN = 3_000, 1_000_000


def _pool(tick, positions, tid):
    return Pool(pool_id=tid, token0="T0", token1="T1", fee_numerator=F_NUM,
                fee_denominator=F_DEN, current_tick=tick, positions=tuple(positions))


def test_wide_range_differential_both_directions() -> None:
    """Positions spanning thousands of ticks, crossing in both directions."""
    rng = random.Random(7)
    edges = sorted(rng.sample(range(-20000, 20001), 8))
    positions = []
    for k in range(len(edges) - 1):
        if rng.random() < 0.7:  # leave occasional gaps
            positions.append(Position(edges[k], edges[k + 1],
                                      rng.choice([1, 10**6, 10**20, 10**40])))
    for case, start in enumerate(edges[:-1]):
        pool = _pool(start, positions, f"wide{case}")
        for zfo in (True, False):
            for amount in (1, 10**30, 10**70):
                r = quote_swap(pool, zero_for_one=zfo, amount_in=amount)
                errs = verify_against_engine(pool, r)
                assert errs == [], (case, zfo, amount, errs)


def test_universe_edge_positions() -> None:
    """Liquidity running exactly to TICK_MIN/TICK_MAX: must stop tick_boundary."""
    pool = _pool(0, [Position(TICK_MIN, TICK_MAX, 10**30)], "edge")
    for zfo in (True, False):
        r = quote_swap(pool, zero_for_one=zfo, amount_in=10**78)
        assert r.stop_reason == "tick_boundary"
        errs = verify_against_engine(pool, r)
        assert errs == [], errs
        # The last crossed segment lands on the universe edge price.
        crossed = [s for s in r.segments if s.crossed]
        assert crossed
        edge_tick = TICK_MIN if zfo else TICK_MAX
        assert int(crossed[-1].end_sqrt_price) == sqrt_price_at_tick(edge_tick)


def test_start_on_universe_edge() -> None:
    """Quoting from TICK_MIN downward / TICK_MAX upward trades nothing."""
    down = _pool(TICK_MIN, [Position(TICK_MIN, TICK_MAX, 10**30)], "emin")
    r = quote_swap(down, zero_for_one=True, amount_in=10**30)
    assert r.stop_reason == "tick_boundary"
    assert int(r.amount_out) == 0
    assert int(r.unspent_input) == 10**30
    assert verify_against_engine(down, r) == []

    up = _pool(TICK_MAX, [Position(TICK_MIN, TICK_MAX, 10**30)], "emax")
    r = quote_swap(up, zero_for_one=False, amount_in=10**30)
    assert r.stop_reason == "tick_boundary"
    assert int(r.amount_out) == 0
    assert int(r.unspent_input) == 10**30
    assert verify_against_engine(up, r) == []


def test_microscopic_gaps() -> None:
    """One-tick gaps must stop precisely and return the unspent input."""
    # Covers [0,100) and [101,200) leaving exactly tick interval [100,101) empty.
    pool = _pool(50, [Position(0, 100, 10**20), Position(101, 200, 10**20)], "micro")
    r = quote_swap(pool, zero_for_one=False, amount_in=10**40)
    assert r.stop_reason == "empty_range"
    assert r.current_tick_after == 100
    total_gross = sum(int(s.gross_input) for s in r.segments)
    assert total_gross + int(r.unspent_input) == 10**40
    assert verify_against_engine(pool, r) == []


def test_minimum_liquidity_one_unit_sweep() -> None:
    """L == 1 across many ticks: every division stays defined and reference agrees."""
    rng = random.Random(99)
    for _ in range(20):
        center = rng.randint(-1000, 1000)
        pool = _pool(center, [Position(center - 50, center + 50, 1)], "l1")
        amount = rng.choice([1, 2, 10**6, 10**12, 10**30])
        zfo = rng.choice([True, False])
        r = quote_swap(pool, zero_for_one=zfo, amount_in=amount)
        assert verify_against_engine(pool, r) == []


def test_tick_map_edges_against_decimal() -> None:
    for t in (TICK_MIN, TICK_MIN + 1, TICK_MAX - 1, TICK_MAX):
        assert sqrt_price_at_tick(t) == reference_sqrt_price(t)
