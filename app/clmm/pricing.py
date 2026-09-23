"""tick 与 sqrtPriceX96 之间的精确整数映射。

规则（项目自定义定点协议）
--------------------------

1. ``P(t) = 1.0001 ** t``，其中 P 是 token1/token0 价格；
   ``sqrtP(t) = (10001/10000) ** (t/2)``。
2. 网格边界（tick 边界）的定点值向上取整::

       sqrtP_X96(t) = ceil( 2**96 * sqrtP(t) )

3. 反查 tick 向下取整::

       tick_of(s) = max { t : sqrtP_X96(t) <= s }

计算策略
--------

边界值先由高精度 Decimal（80 位有效数字）求出候选，再做精确整数校正：

* ``|tick| <= 50_000``：直接构造 ``10001**t`` 等整数，用
  ``s*s*den >= 2**192*num`` 精确校验并上下修正，结果有严格数学保证；
* 更大的 tick：极值价格约 1e38，80 位 Decimal 的绝对误差远小于 1 个
  整数单位（相对误差 <= 1e-40），候选即精确 ceil，无需构造数百万位整数。

边界一律取 ceil：换入容量按 ceil、换出按 floor（见 engine.py），任何
舍入都不会让池子吃亏。所有对外结果只用整数，禁止浮点参与报价。
"""

from decimal import ROUND_CEILING, Decimal, localcontext

from .constants import (
    MAX_TICK,
    MIN_TICK,
    Q96_SCALE,
    TICK_SPACING_BASE_DEN,
    TICK_SPACING_BASE_NUM,
)

_SMALL_TICK = 50_000
_DECIMAL_PREC = 80
_SQRT_Q192 = Q96_SCALE * Q96_SCALE
_Q96D = Decimal(Q96_SCALE)
_BASE = Decimal(TICK_SPACING_BASE_NUM) / Decimal(TICK_SPACING_BASE_DEN)

_boundary_cache: dict[int, int] = {}

#: sqrtPriceX96 合法闭区间（模块加载后赋值）
MIN_SQRT_X96 = 0
MAX_SQRT_X96 = 0


def tick_sqrt_x96(tick: int) -> int:
    """返回 tick 边界的 sqrtPriceX96，精确向上取整（ceil）。"""
    cached = _boundary_cache.get(tick)
    if cached is not None:
        return cached
    if not isinstance(tick, int):
        raise TypeError("tick 必须是整数")
    if not MIN_TICK <= tick <= MAX_TICK:
        raise ValueError(f"tick 超出允许范围 [{MIN_TICK}, {MAX_TICK}]: {tick}")

    s = _boundary_candidate(tick)
    s = _exact_correct(tick, s)
    _boundary_cache[tick] = s
    return s


def _boundary_candidate(tick: int) -> int:
    """高精度 Decimal 求 ceil(2^96 * (10001/10000)**(tick/2))。"""
    with localcontext() as ctx:
        ctx.prec = _DECIMAL_PREC
        val = _Q96D * (_BASE ** (Decimal(tick) / 2))
        return int(val.to_integral_value(rounding=ROUND_CEILING))


def _exact_correct(tick: int, s: int) -> int:
    """用精确整数关系把候选校正为真正的最小满足者。

    精确关系（tick>=0）：``s*s * 10000**t >= 2**192 * 10001**t``；
    tick<0 时分子分母互换。``|tick| > _SMALL_TICK`` 时构造两端需要
    数百万位整数，而 Decimal 候选的绝对误差远小于 1，直接采用。
    """
    at = abs(tick)
    if at > _SMALL_TICK:
        return s
    a = TICK_SPACING_BASE_NUM ** at
    b = TICK_SPACING_BASE_DEN ** at
    if tick >= 0:
        target_num, den = _SQRT_Q192 * a, b
    else:
        target_num, den = _SQRT_Q192 * b, a
    while s * s * den < target_num:
        s += 1
    while s > 1 and (s - 1) * (s - 1) * den >= target_num:
        s -= 1
    return s


def sqrt_x96_to_tick(s: int) -> int:
    """由 sqrtPriceX96 反查当前 tick，向下取整。

    返回最大的满足 ``tick_sqrt_x96(t) <= s`` 的整数 tick。
    """
    _validate_sqrt_x96(s)
    lo, hi = MIN_TICK, MAX_TICK + 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if tick_sqrt_x96(mid) <= s:
            lo = mid
        else:
            hi = mid
    return lo


def _validate_sqrt_x96(s: int) -> None:
    if not isinstance(s, int):
        raise TypeError("sqrtPriceX96 必须是整数")
    if s < MIN_SQRT_X96 or s > MAX_SQRT_X96:
        raise ValueError(
            f"sqrtPriceX96 超出允许范围 [{MIN_SQRT_X96}, {MAX_SQRT_X96}]: {s}"
        )


def is_at_tick_boundary(s: int) -> bool:
    """s 是否精确落在某个 tick 边界上。"""
    _validate_sqrt_x96(s)
    return tick_sqrt_x96(sqrt_x96_to_tick(s)) == s


MIN_SQRT_X96 = tick_sqrt_x96(MIN_TICK)
MAX_SQRT_X96 = tick_sqrt_x96(MAX_TICK)
