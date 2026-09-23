"""Decimal 慢速参考 vs 整数引擎的交叉验证。

覆盖任务要求的四类场景：正反向跨界、边界 tick、极小流动性、最大输入。
grid=True 时两套独立实现必须逐段完全一致；grid=False（连续模型）时
引擎输出不得高于连续模型，且每段低估量严格有界（不超过舍入误差）。
"""

import random

import pytest

from app.clmm.constants import MAX_TICK, MIN_TICK
from app.clmm.decimalref import reference_swap
from app.clmm.engine import Pool, Position, quote_swap
from app.clmm.pricing import tick_sqrt_x96

Q96 = 1 << 96


def assert_exact_match(pool, zfo, amount, limit_tick=None):
    eng = quote_swap(pool, zfo, amount, limit_tick)
    ref = reference_swap(pool, zfo, amount, limit_tick, grid=True)
    assert eng.stop_reason == ref.stop_reason
    assert eng.stop_tick == ref.stop_tick
    assert eng.amount_out_total == ref.total_out
    assert eng.amount_in_unfilled == ref.unfilled
    assert eng.fee_total == ref.fee_total
    assert len(eng.segments) == len(ref.segments)
    for es, rs in zip(eng.segments, ref.segments):
        assert int(es.liquidity) == rs.liquidity
        assert int(es.fee) == rs.fee
        assert int(es.principal_in) == rs.principal
        assert int(es.amount_out) == rs.out
        assert int(es.amount_out) >= 0
        assert es.ended_by == rs.ended_by
        assert int(es.sqrt_price_end_x96) == int(rs.q_end * Q96)


def test_forward_and_reverse_cross_multiple_bands():
    pool = Pool(
        pool_id="x", token0="A", token1="B", fee_ppm=3000,
        sqrt_price_x96=tick_sqrt_x96(0),
        positions=(
            Position(-20, -5, 10**17),
            Position(-5, 5, 4 * 10**17),
            Position(5, 25, 2 * 10**17),
        ),
    )
    # 选能跨至少两个边界的输入量（正反两个方向）
    assert_exact_match(pool, False, 5 * 10**18)   # token1->token0 上行跨界
    assert_exact_match(pool, True, 5 * 10**18)    # token0->token1 下行跨界


@pytest.mark.parametrize("t", [MIN_TICK + 30, -7, -1, 0, 1, 7, MAX_TICK - 30])
def test_starting_on_boundary_ticks_both_directions(t):
    pool = Pool(
        pool_id="b", token0="A", token1="B", fee_ppm=1234,
        sqrt_price_x96=tick_sqrt_x96(t),
        positions=(Position(t - 20, t + 20, 7 * 10**15),),
    )
    assert_exact_match(pool, True, 10**14)
    assert_exact_match(pool, False, 10**14)
    assert_exact_match(pool, True, 10**20)
    assert_exact_match(pool, False, 10**20)


def test_tiny_liquidity_matches_reference():
    pool = Pool(
        pool_id="tiny", token0="A", token1="B", fee_ppm=3000,
        sqrt_price_x96=tick_sqrt_x96(0),
        positions=(Position(-5, 5, 1),),
    )
    for amount in (1, 2, 10**3, 10**12):
        assert_exact_match(pool, True, amount)
        assert_exact_match(pool, False, amount)


def test_maximum_input_matches_reference():
    pool = Pool(
        pool_id="max", token0="A", token1="B", fee_ppm=3000,
        sqrt_price_x96=tick_sqrt_x96(0),
        positions=(Position(MIN_TICK, MAX_TICK, 10**40),),
    )
    assert_exact_match(pool, True, 10**70)
    assert_exact_match(pool, False, 10**70)


def test_limit_tick_matches_reference():
    pool = Pool(
        pool_id="lim", token0="A", token1="B", fee_ppm=3000,
        sqrt_price_x96=tick_sqrt_x96(0),
        positions=(Position(MIN_TICK, MAX_TICK, 10**18),),
    )
    assert_exact_match(pool, True, 10**30, limit_tick=-9)
    assert_exact_match(pool, False, 10**30, limit_tick=9)


def test_near_full_fee_tiny_input_zero_principal_no_negative_output():
    """回归：fee_ppm=999999 且输入 1 -> 本金 0，输出必须夹零、终价不移动。"""
    pool = Pool(
        pool_id="z", token0="A", token1="B", fee_ppm=999999,
        sqrt_price_x96=tick_sqrt_x96(-146),
        positions=(
            Position(-153, -34, 1),
            Position(-230, -25, 563_701_493_091),
            Position(184, 286, 617_889_007_361),
        ),
    )
    for zfo in (True, False):
        assert_exact_match(pool, zfo, 1)
        r = quote_swap(pool, zfo, 1)
        assert r.fee_total == 1
        assert r.amount_out_total == 0
        assert all(int(s.amount_out) >= 0 for s in r.segments)
        assert r.sqrt_price_end_x96 == r.sqrt_price_start_x96


def test_randomized_pools_vs_reference():
    rng = random.Random(20260922)
    for case in range(60):
        # 随机构造 1-4 个仓位（允许重叠）
        n = rng.randint(1, 4)
        positions = []
        for _ in range(n):
            a = rng.randint(-200, 200)
            b = a + rng.randint(1, 200)
            positions.append(Position(a, b, rng.randint(1, 10**9)))
        start_tick = rng.randint(-220, 220)
        try:
            pool = Pool(
                pool_id=f"r{case}", token0="A", token1="B",
                fee_ppm=rng.choice([0, 1, 500, 3000, 999999]),
                sqrt_price_x96=tick_sqrt_x96(start_tick),
                positions=tuple(positions),
            )
        except ValueError:
            continue
        zfo = rng.choice([True, False])
        amount = rng.choice([1, 2, 10**rng.randint(0, 18), 10**24])
        assert_exact_match(pool, zfo, amount)


def test_continuous_reference_bounds_integer_engine():
    """连续 Decimal 模型（grid=False）：引擎的整数输出不得高于连续值，
    且每个 filled 段低估不超过 q 网格舍入造成的严格上界。"""
    pool = Pool(
        pool_id="c", token0="A", token1="B", fee_ppm=3000,
        sqrt_price_x96=tick_sqrt_x96(0),
        positions=(Position(-50, 50, 5 * 10**17),),
    )
    for zfo in (True, False):
        amount = 10**12  # 该规模下价格移动不足一个 tick，保证单段 filled
        eng = quote_swap(pool, zfo, amount)
        ref = reference_swap(pool, zfo, amount, grid=False)
        assert eng.stop_reason == "filled"
        # 总输出 floor(连续) >= 引擎总输出（引擎价格舍入更保守）
        assert ref.total_out >= eng.amount_out_total
        # 单段场景：低估量严格小于 2（floor 取整 + 一格 sqrtPrice 舍入）
        assert len(eng.segments) == 1
        assert ref.total_out - eng.amount_out_total < 2
