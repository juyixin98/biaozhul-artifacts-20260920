"""引擎单元测试：费用舍入、对账恒等式、空区间、除零防护、边界 tick、
极小流动性、最大输入、limit tick、快照不变性。"""

import copy

import pytest

from app.clmm.constants import (
    FEE_DENOMINATOR,
    MAX_TICK,
    MIN_TICK,
    STOP_FILLED,
    STOP_INCOMPLETE_EDGE,
    STOP_INCOMPLETE_GAP,
    STOP_INCOMPLETE_LIMIT,
)
from app.clmm.engine import (
    Pool,
    Position,
    ceil_div,
    fee_on,
    quote_swap,
)
from app.clmm.pricing import MAX_SQRT_X96, MIN_SQRT_X96, tick_sqrt_x96


def make_pool(**over):
    kw = dict(
        pool_id="p",
        token0="A",
        token1="B",
        fee_ppm=3000,
        sqrt_price_x96=tick_sqrt_x96(0),
        positions=(Position(-10, 10, 10**18),),
    )
    kw.update(over)
    return Pool(**kw)


# ------------------------- 舍入与对账 -------------------------


def test_fee_is_ceil():
    # 3000 ppm = 0.3%；不能整除时必须向上
    assert fee_on(1, 3000) == 1                 # ceil(0.0003)
    assert fee_on(10000, 3000) == 30
    assert fee_on(7, 1) == 1                    # ceil(7/1e6)
    assert fee_on(0, 3000) == 0


def test_ceil_div_basic():
    assert ceil_div(0, 3) == 0
    assert ceil_div(7, 3) == 3
    assert ceil_div(6, 3) == 2
    with pytest.raises(ZeroDivisionError):
        ceil_div(1, 0)


@pytest.mark.parametrize("zero_for_one", [True, False])
def test_input_reconciliation_identity(zero_for_one):
    pool = make_pool()
    r = quote_swap(pool, zero_for_one, 123456789)
    principal = sum(int(s.principal_in) for s in r.segments)
    fees = sum(int(s.fee) for s in r.segments)
    assert r.amount_in == fees + principal + r.amount_in_unfilled
    assert r.to_dict()["input_reconciled"] is True


def test_zero_fee_pool():
    pool = make_pool(
        fee_ppm=0,
        positions=(Position(MIN_TICK, MAX_TICK, 10**18),),
    )
    r = quote_swap(pool, True, 10**15)
    assert r.fee_total == 0
    assert all(int(s.fee) == 0 for s in r.segments)
    assert r.amount_in_unfilled == 0


def test_minimum_input_rounds_all_to_fee():
    # 3000 ppm，输入 1 -> fee ceil=1，principal=0，输出 0，资金全部记账为费用
    r = quote_swap(make_pool(), True, 1)
    assert r.fee_total == 1
    assert r.amount_out_total == 0
    assert r.stop_reason == STOP_FILLED
    assert r.amount_in_unfilled == 0


# ------------------------- 空区间与除零 -------------------------


def test_gap_immediately_ahead_stops_without_consuming_input():
    # 当前 tick 2：下方 [-10,0) 与当前区间不相交，[5,15) 在上方 -> 当前就是空区间
    pool = Pool(
        pool_id="g", token0="A", token1="B", fee_ppm=3000,
        sqrt_price_x96=tick_sqrt_x96(2),
        positions=(Position(-10, 0, 10**18), Position(5, 15, 10**18)),
    )
    r = quote_swap(pool, True, 10**30)
    assert r.stop_reason == STOP_INCOMPLETE_GAP
    assert r.amount_in_unfilled == 10**30
    assert r.fee_total == 0
    assert r.amount_out_total == 0
    assert r.segments == []


def test_gap_after_crossing_one_band():
    # 在 tick 2 有流动性（[-10,3)），穿到 tick -1 后进入空区间 (-10 以下空)…
    pool = Pool(
        pool_id="g2", token0="A", token1="B", fee_ppm=3000,
        sqrt_price_x96=tick_sqrt_x96(2),
        positions=(Position(0, 3, 10**15),),
    )
    r = quote_swap(pool, True, 10**30)
    assert r.stop_reason == STOP_INCOMPLETE_GAP
    assert r.stop_tick == -1
    # 第一段有成交与费用，其余输入原样返回，不被收费
    assert r.segments
    assert r.amount_in_unfilled > 0
    assert r.amount_in_unfilled == r.amount_in - sum(
        int(s.amount_in_gross) for s in r.segments
    )


def test_fee_100_percent_rejected():
    with pytest.raises(ValueError):
        make_pool(fee_ppm=FEE_DENOMINATOR)


def test_invalid_fee_and_amounts():
    with pytest.raises(ValueError):
        make_pool(fee_ppm=-1)
    with pytest.raises(ValueError):
        quote_swap(make_pool(), True, 0)
    with pytest.raises(ValueError):
        quote_swap(make_pool(), False, -5)


def test_empty_positions_rejected():
    with pytest.raises(ValueError):
        Pool(pool_id="x", token0="A", token1="B", fee_ppm=0,
             sqrt_price_x96=tick_sqrt_x96(0), positions=()).validate()


def test_overlapping_positions_aggregate_and_never_divide_zero():
    # 完全重叠仓位聚合；段内 L 恒定为两者之和
    pool = make_pool(positions=(
        Position(-10, 10, 10**18), Position(-5, 5, 2 * 10**18)
    ))
    r = quote_swap(pool, False, 10**16)
    assert int(r.segments[0].liquidity) == 3 * 10**18


# ------------------------- 边界 tick / 极值 / 极小流动性 -------------------------


@pytest.mark.parametrize("start_tick", [MIN_TICK, -1, 0, 1, MAX_TICK - 1])
def test_starting_exactly_on_tick_boundaries(start_tick):
    pool = Pool(
        pool_id="b", token0="A", token1="B", fee_ppm=500,
        sqrt_price_x96=tick_sqrt_x96(start_tick),
        positions=(Position(MIN_TICK, MAX_TICK, 10**12),),
    )
    r_down = quote_swap(pool, True, 10**12)
    r_up = quote_swap(pool, False, 10**12)
    assert r_down.amount_in == r_down.fee_total + sum(
        int(s.principal_in) for s in r_down.segments
    ) + r_down.amount_in_unfilled
    assert r_up.amount_in == r_up.fee_total + sum(
        int(s.principal_in) for s in r_up.segments
    ) + r_up.amount_in_unfilled


def test_min_tick_blocks_down_swap_at_edge():
    pool = Pool(pool_id="e", token0="A", token1="B", fee_ppm=0,
                sqrt_price_x96=MIN_SQRT_X96,
                positions=(Position(MIN_TICK, MAX_TICK, 10**12),))
    r = quote_swap(pool, True, 10**18)
    assert r.stop_reason == STOP_INCOMPLETE_EDGE
    assert r.stop_tick == MIN_TICK
    assert r.amount_in_unfilled == 10**18


def test_max_tick_blocks_up_swap_at_edge():
    pool = Pool(pool_id="e2", token0="A", token1="B", fee_ppm=0,
                sqrt_price_x96=MAX_SQRT_X96,
                positions=(Position(MIN_TICK, MAX_TICK, 10**12),))
    r = quote_swap(pool, False, 10**18)
    assert r.stop_reason == STOP_INCOMPLETE_EDGE
    assert r.stop_tick == MAX_TICK
    assert r.amount_in_unfilled == 10**18


def test_tiny_liquidity_one_unit():
    # L=1，大输入几乎立即跨界；必须不抛除零错误、逐段对账
    pool = Pool(pool_id="L1", token0="A", token1="B", fee_ppm=3000,
                sqrt_price_x96=tick_sqrt_x96(0),
                positions=(Position(-3, 3, 1),))
    for zfo in (True, False):
        r = quote_swap(pool, zfo, 10**40)
        assert r.stop_reason == STOP_INCOMPLETE_GAP
        assert r.amount_in == r.fee_total + sum(
            int(s.principal_in) for s in r.segments
        ) + r.amount_in_unfilled


def test_maximum_input_does_not_overflow_or_divide_zero():
    pool = Pool(pool_id="big", token0="A", token1="B", fee_ppm=3000,
                sqrt_price_x96=tick_sqrt_x96(0),
                positions=(Position(MIN_TICK, MAX_TICK, 10**40),))
    huge = 10**70
    r = quote_swap(pool, True, huge)
    assert 0 < r.amount_out_total
    assert r.stop_reason in (STOP_FILLED, STOP_INCOMPLETE_EDGE)
    assert r.amount_in == r.fee_total + sum(
        int(s.principal_in) for s in r.segments
    ) + r.amount_in_unfilled


# ------------------------- limit tick -------------------------


def test_limit_tick_stops_before_target_down():
    pool = make_pool(positions=(Position(MIN_TICK, MAX_TICK, 10**18),))
    r = quote_swap(pool, True, 10**30, limit_tick=-5)
    assert r.stop_reason == STOP_INCOMPLETE_LIMIT
    assert r.stop_tick == -5
    assert r.sqrt_price_end_x96 == tick_sqrt_x96(-5)
    assert r.amount_in_unfilled > 0


def test_limit_tick_stops_up():
    pool = make_pool(positions=(Position(MIN_TICK, MAX_TICK, 10**18),))
    r = quote_swap(pool, False, 10**30, limit_tick=7)
    assert r.stop_reason == STOP_INCOMPLETE_LIMIT
    assert r.stop_tick == 7


def test_limit_tick_wrong_side_rejected():
    pool = make_pool()
    with pytest.raises(ValueError):
        quote_swap(pool, True, 1, limit_tick=5)
    with pytest.raises(ValueError):
        quote_swap(pool, False, 1, limit_tick=-5)


# ------------------------- 多段跨界 -------------------------


def test_multi_band_cross_records_each_band():
    pool = Pool(
        pool_id="m", token0="A", token1="B", fee_ppm=3000,
        sqrt_price_x96=tick_sqrt_x96(0),
        positions=(
            Position(-10, 0, 10**18),
            Position(0, 10, 3 * 10**18),
            Position(10, 20, 9 * 10**18),
        ),
    )
    r = quote_swap(pool, False, 10**24)
    crossed = [s.crossed_tick for s in r.segments if s.ended_by == "cross"]
    assert 10 in crossed
    liqs = [int(s.liquidity) for s in r.segments]
    assert liqs[0] == 3 * 10**18
    assert 9 * 10**18 in liqs
    # 每段都有真实费用（禁止跳过成本）
    assert all(int(s.fee) > 0 for s in r.segments)
    # 段价格单调
    for s in r.segments:
        assert int(s.sqrt_price_start_x96) <= int(s.sqrt_price_end_x96)


# ------------------------- 快照不可变 -------------------------


def test_all_amounts_non_negative_never_skip_cost():
    """任何报价结果与每段证据都不允许负输出；跨界段必须留下费用（不跳过成本）。"""
    pool = Pool(
        pool_id="n", token0="A", token1="B", fee_ppm=3000,
        sqrt_price_x96=tick_sqrt_x96(0),
        positions=(Position(-20, 0, 10**15), Position(0, 20, 4 * 10**15)),
    )
    for zfo in (True, False):
        for amt in (1, 10**6, 10**18, 10**30):
            r = quote_swap(pool, zfo, amt)
            assert r.amount_out_total >= 0
            assert r.fee_total >= 0
            assert r.amount_in_unfilled >= 0
            for s in r.segments:
                assert int(s.amount_out) >= 0
                # 只要该段真的消耗了毛输入且费率非零，费用必须为正
                if int(s.amount_in_gross) > 0:
                    assert int(s.fee) > 0


def test_quote_does_not_mutate_pool_snapshot():
    pool = make_pool()
    snap_before = copy.deepcopy(pool)
    quote_swap(pool, True, 10**18)
    quote_swap(pool, False, 10**18)
    assert pool == snap_before
