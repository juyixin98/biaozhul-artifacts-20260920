"""价格映射测试：整数 tick 网格、ceil 边界、floor 反查、单调性、极值。"""

from decimal import Decimal

import pytest

from app.clmm.constants import MAX_TICK, MIN_TICK, Q96_SCALE
from app.clmm.pricing import (
    MAX_SQRT_X96,
    MIN_SQRT_X96,
    is_at_tick_boundary,
    sqrt_x96_to_tick,
    tick_sqrt_x96,
)


def test_tick_zero_is_exact_one():
    assert tick_sqrt_x96(0) == Q96_SCALE


def test_boundary_is_ceil_of_real_value():
    # sqrtPriceX96(t) = ceil(2^96 * sqrt(1.0001^t))，用高精度 Decimal 独立核验
    from decimal import localcontext

    real = Decimal(10001) / Decimal(10000)
    with localcontext() as ctx:
        ctx.prec = 100
        for t in (1, -1, 2, -2, 777, -777, 50000, -50000, 887272, -887272):
            val = Q96_SCALE * (real ** (Decimal(t) / 2))
            expected = int(val.to_integral_value(rounding="ROUND_CEILING"))
            assert tick_sqrt_x96(t) == expected, t


def test_boundary_cache_stable():
    assert tick_sqrt_x96(12345) == tick_sqrt_x96(12345)


def test_monotonic_strictly_increasing():
    ts = [MIN_TICK, -100000, -1, 0, 1, 100000, MAX_TICK]
    vals = [tick_sqrt_x96(t) for t in ts]
    assert all(a < b for a, b in zip(vals, vals[1:]))


def test_tick_roundtrip_floor_semantics():
    for t in (-100, -1, 0, 1, 100):
        s = tick_sqrt_x96(t)
        assert sqrt_x96_to_tick(s) == t
        # 边界上方 1 个单位仍在同一 tick 内（除非被 ceil 造成间隙，间隙点属于下一 tick）
        inside = sqrt_x96_to_tick(s + 1)
        assert inside in (t, t + 1)


def test_sqrt_to_tick_inside_interval():
    # 任意合法 s：tick_sqrt(t) <= s < tick_sqrt(t+1)
    for s in (MIN_SQRT_X96, Q96_SCALE, Q96_SCALE + 1, MAX_SQRT_X96 - 1, MAX_SQRT_X96):
        t = sqrt_x96_to_tick(s)
        assert tick_sqrt_x96(t) <= s
        if t < MAX_TICK:
            assert s < tick_sqrt_x96(t + 1)


def test_boundary_predicate():
    assert is_at_tick_boundary(Q96_SCALE)
    assert not is_at_tick_boundary(Q96_SCALE + 123)


def test_extreme_tick_ranges():
    assert tick_sqrt_x96(MIN_TICK) == MIN_SQRT_X96 > 0
    assert tick_sqrt_x96(MAX_TICK) == MAX_SQRT_X96
    # 极值价格在 Decimal 下确实远离 0/无穷（不会除零）
    assert Decimal(MIN_SQRT_X96) ** 2 / Decimal(Q96_SCALE) ** 2 > 0
    assert Decimal(MAX_SQRT_X96) ** 2 / Decimal(Q96_SCALE) ** 2 < Decimal(10) ** 80


def test_reject_out_of_range_tick():
    with pytest.raises(ValueError):
        tick_sqrt_x96(MIN_TICK - 1)
    with pytest.raises(ValueError):
        tick_sqrt_x96(MAX_TICK + 1)


def test_reject_bad_sqrt():
    with pytest.raises(ValueError):
        sqrt_x96_to_tick(MIN_SQRT_X96 - 1)
    with pytest.raises(ValueError):
        sqrt_x96_to_tick(MAX_SQRT_X96 + 1)
