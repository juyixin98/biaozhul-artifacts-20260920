"""纯函数集中流动性报价引擎。

设计要点
========

* 池快照（:class:`Pool`）不可变，报价不修改任何储备；引擎层不接触数据库。
* 流动性在整数 tick 网格上分段恒定：仓位 ``Position`` 在半开 tick 区间
  ``[lower_tick, upper_tick)`` 内提供 ``liquidity``。
* 一次交换可跨越任意多个流动性区间，逐段：

  1. 先从该段毛输入中扣费（ceil，对协议有利），其余为本金（floor）；
  2. 用恒定乘积公式（x*y=k 的 sqrtPrice 形式）求价格移动与输出；
  3. 到 tick 边界时结算该段，按下一区间流动性继续；
  4. 前方区间流动性为 0（空区间）则立即停止，未消耗输入原样返回。

舍入方向（项目自定义定点规则，全部对池子/协议有利，杜绝免费套利）
-----------------------------------------------------------------

========================== ========================================
量                         舍入
========================== ========================================
段费用 fee                  ceil（向上）
段本金 principal            floor（= 毛输入 - fee）
跨 tick 所需本金容量        ceil（向上）
段输出数量 amount_out       floor（向下）
部分成交后的 sqrtPrice      朝段内夹持：下行 ceil、上行 floor（不越过真实终点）
未成交输入                  原样返回（不扣费）
========================== ========================================

恒等式（每段与总计都必须成立）::

    amount_in == Σ(fee) + Σ(principal) + amount_in_unfilled

零分母防护：fee_ppm == FEE_DENOMINATOR（100% 费用）在建池时拒绝；
L == 0 的区间不进入除法，直接按空区间停止；所有 sqrtPrice 均为正整数。
"""

from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from typing import Optional

from .constants import (
    FEE_DENOMINATOR,
    MAX_SWAP_STEPS,
    MAX_TICK,
    MIN_TICK,
    Q96_SCALE,
    STOP_FILLED,
    STOP_INCOMPLETE_EDGE,
    STOP_INCOMPLETE_GAP,
    STOP_INCOMPLETE_LIMIT,
)
from .pricing import MAX_SQRT_X96, MIN_SQRT_X96, sqrt_x96_to_tick, tick_sqrt_x96

getcontext().prec = 80


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Position:
    """一个集中流动性仓位，在 [lower_tick, upper_tick) 内贡献 liquidity。"""

    lower_tick: int
    upper_tick: int
    liquidity: int

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not (isinstance(self.lower_tick, int) and isinstance(self.upper_tick, int)):
            raise ValueError("tick 必须为整数")
        if not (MIN_TICK <= self.lower_tick < self.upper_tick <= MAX_TICK):
            raise ValueError(
                f"仓位 tick 区间非法，要求 "
                f"{MIN_TICK} <= lower < upper <= {MAX_TICK}: "
                f"[{self.lower_tick}, {self.upper_tick})"
            )
        if not isinstance(self.liquidity, int) or self.liquidity <= 0:
            raise ValueError("仓位流动性 liquidity 必须为正整数")


@dataclass(frozen=True)
class Pool:
    """不可变池快照。报价只读此对象，绝不修改。"""

    pool_id: str
    token0: str
    token1: str
    fee_ppm: int
    sqrt_price_x96: int
    positions: tuple[Position, ...]

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.pool_id or not isinstance(self.pool_id, str):
            raise ValueError("pool_id 非法")
        if not self.token0 or not self.token1 or self.token0 == self.token1:
            raise ValueError("token0/token1 必须为两个不同的非空符号")
        if not isinstance(self.fee_ppm, int) or not (0 <= self.fee_ppm < FEE_DENOMINATOR):
            # fee_ppm == FEE_DENOMINATOR 意味着 100% 费用、principal 恒为 0，
            # 属于被禁止的退化情形（除零/无意义报价）。
            raise ValueError(
                f"fee_ppm 必须为 [{0}, {FEE_DENOMINATOR - 1}] 内整数"
            )
        if not isinstance(self.sqrt_price_x96, int):
            raise ValueError("sqrt_price_x96 必须为整数")
        if not (MIN_SQRT_X96 <= self.sqrt_price_x96 <= MAX_SQRT_X96):
            raise ValueError("sqrt_price_x96 超出 tick 网格范围")
        if not self.positions:
            raise ValueError("池至少需要一个仓位")
        for p in self.positions:
            p.validate()
        _validate_liquidity_bands(self.positions)


def _validate_liquidity_bands(positions: tuple[Position, ...]) -> None:
    """扫描所有事件 tick，保证任一网格区间内聚合流动性非负。"""
    deltas: dict[int, int] = {}
    for p in positions:
        deltas[p.lower_tick] = deltas.get(p.lower_tick, 0) + p.liquidity
        deltas[p.upper_tick] = deltas.get(p.upper_tick, 0) - p.liquidity
    active = 0
    for t in sorted(deltas):
        # t 之后的新区间 [t, next) 流动性为 active + deltas[t]
        active += deltas[t]
        if active < 0:
            raise ValueError(f"tick {t} 之后聚合流动性为负，仓位集合非法")


@dataclass
class Segment:
    """逐段证据：一段恒定流动性内的实际成交。"""

    segment_index: int
    tick_lo: int
    tick_hi: int
    liquidity: str
    sqrt_price_start_x96: str
    sqrt_price_end_x96: str
    price_start: str          # token1/token0，Decimal 展示用，不参与计算
    price_end: str
    amount_in_gross: str
    fee: str
    principal_in: str
    amount_out: str
    ended_by: str             # partial | cross | stop_gap | stop_edge | stop_limit
    crossed_tick: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "segment_index": self.segment_index,
            "tick_lo": self.tick_lo,
            "tick_hi": self.tick_hi,
            "liquidity": self.liquidity,
            "sqrt_price_start_x96": self.sqrt_price_start_x96,
            "sqrt_price_end_x96": self.sqrt_price_end_x96,
            "price_start": self.price_start,
            "price_end": self.price_end,
            "amount_in_gross": self.amount_in_gross,
            "fee": self.fee,
            "principal_in": self.principal_in,
            "amount_out": self.amount_out,
            "ended_by": self.ended_by,
            "crossed_tick": self.crossed_tick,
        }


@dataclass
class QuoteResult:
    direction: str
    token_in: str
    token_out: str
    amount_in: int
    amount_in_unfilled: int
    amount_out_total: int
    fee_total: int
    stop_reason: str
    stop_tick: Optional[int]
    sqrt_price_start_x96: int
    sqrt_price_end_x96: int
    tick_start: int
    tick_end: int
    segments: list[Segment] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "token_in": self.token_in,
            "token_out": self.token_out,
            "amount_in": str(self.amount_in),
            "amount_in_unfilled": str(self.amount_in_unfilled),
            "amount_out_total": str(self.amount_out_total),
            "fee_total": str(self.fee_total),
            "stop_reason": self.stop_reason,
            "stop_tick": self.stop_tick,
            "sqrt_price_start_x96": str(self.sqrt_price_start_x96),
            "sqrt_price_end_x96": str(self.sqrt_price_end_x96),
            "tick_start": self.tick_start,
            "tick_end": self.tick_end,
            "segments": [s.to_dict() for s in self.segments],
            "input_reconciled": (
                self.amount_in
                == self.fee_total
                + sum(int(s.principal_in) for s in self.segments)
                + self.amount_in_unfilled
            ),
        }


# --------------------------------------------------------------------------- #
# 定点小工具
# --------------------------------------------------------------------------- #


def ceil_div(a: int, b: int) -> int:
    """向上取整除法，a>=0, b>0。"""
    return -((-a) // b)


def fee_on(amount: int, fee_ppm: int) -> int:
    """对输入收取的费用，ceil 取整（对协议有利）。"""
    return ceil_div(amount * fee_ppm, FEE_DENOMINATOR)


def _active_liquidity(tick: int, positions: tuple[Position, ...]) -> int:
    """状态 tick 处的聚合流动性（仓位在 [lower, upper) 内有效）。"""
    return sum(
        p.liquidity for p in positions if p.lower_tick <= tick < p.upper_tick
    )


def _delta_map(positions: tuple[Position, ...]) -> dict[int, int]:
    deltas: dict[int, int] = {}
    for p in positions:
        deltas[p.lower_tick] = deltas.get(p.lower_tick, 0) + p.liquidity
        deltas[p.upper_tick] = deltas.get(p.upper_tick, 0) - p.liquidity
    return deltas


def _price_str(s: int) -> str:
    """展示用：sqrtPriceX96 -> token1/token0 十进制价格（不参与任何计算）。"""
    return str((Decimal(s) ** 2) / (Decimal(Q96_SCALE) ** 2))


# --------------------------------------------------------------------------- #
# 恒定乘积公式（sqrtPrice 形式），全部整数、显式舍入方向
#
#   token0 数量 Δx 与 token1 数量 Δy 满足：
#     向上（token1 换 token0）：Δx = L (s1 - s0) / 2^96
#             所需 token1 本金 = L 2^96 (s1 - s0) / (s0 s1)
#     向下（token0 换 token1）：所需 token0 本金 = L (s0 - s1) / 2^96
#             输出 token1     = L 2^96 (s0 - s1) / (s0 s1)
# --------------------------------------------------------------------------- #


def _principal_cap_up(L: int, s0: int, s1: int) -> int:
    """向上走到 s1 需要的 token1 本金，ceil。"""
    return ceil_div(L * Q96_SCALE * (s1 - s0), s0 * s1)


def _principal_cap_down(L: int, s0: int, s1: int) -> int:
    """向下走到 s1 需要的 token0 本金，ceil。"""
    return ceil_div(L * (s0 - s1), Q96_SCALE)


def _out_up(L: int, s0: int, s1: int) -> int:
    """向上段输出的 token0，floor；principal=0 时终点被夹回 s0，输出必须 >=0。"""
    return max(0, L * (s1 - s0) // Q96_SCALE)


def _out_down(L: int, s0: int, s1: int) -> int:
    """向下段输出的 token1，floor；同理夹零防止负输出。"""
    return max(0, L * Q96_SCALE * (s0 - s1) // (s0 * s1))


def _partial_s_up(L: int, s0: int, principal: int) -> int:
    """给定 token1 本金，向上段的终点 sqrtPrice，floor（不越界）。"""
    # s' = L*2^96*s0 / (L*2^96 - principal*s0)
    return (L * Q96_SCALE * s0) // (L * Q96_SCALE - principal * s0)


def _partial_s_down(L: int, s0: int, principal: int) -> int:
    """给定 token0 本金，向下段的终点 sqrtPrice，floor（价格不多跌）。"""
    return s0 - principal * Q96_SCALE // L


# --------------------------------------------------------------------------- #
# 报价主入口
# --------------------------------------------------------------------------- #


def quote_swap(
    pool: Pool,
    zero_for_one: bool,
    amount_in: int,
    limit_tick: Optional[int] = None,
) -> QuoteResult:
    """对不可变池快照报价。纯函数：不修改 pool，不接触数据库。

    参数
    ----
    pool:          池快照
    zero_for_one:  True = token0 -> token1（价格下行）；False = token1 -> token0
    amount_in:     输入数量（正整数，最小单位）
    limit_tick:    可选，价格不得越过该 tick 边界
    """
    pool.validate()
    if not isinstance(amount_in, int) or amount_in <= 0:
        raise ValueError("amount_in 必须为正整数")
    if limit_tick is not None and not (
        isinstance(limit_tick, int) and MIN_TICK <= limit_tick <= MAX_TICK
    ):
        raise ValueError(f"limit_tick 必须在 [{MIN_TICK}, {MAX_TICK}] 内")

    if zero_for_one:
        return _swap_down(pool, amount_in, limit_tick)
    return _swap_up(pool, amount_in, limit_tick)


def _check_limit_up(limit_tick: Optional[int], s: int) -> None:
    if limit_tick is not None and tick_sqrt_x96(limit_tick) <= s:
        raise ValueError("limit_tick 必须位于价格上行方向（其边界严格高于当前价）")


def _check_limit_down(limit_tick: Optional[int], s: int) -> None:
    if limit_tick is not None and tick_sqrt_x96(limit_tick) >= s:
        raise ValueError("limit_tick 必须位于价格下行方向（其边界严格低于当前价）")


def _swap_up(pool: Pool, amount_in: int, limit_tick: Optional[int]) -> QuoteResult:
    """token1 -> token0，价格沿 tick 网格上行。"""
    _check_limit_up(limit_tick, pool.sqrt_price_x96)
    positions = pool.positions
    deltas = _delta_map(positions)
    events = sorted(deltas)

    s = pool.sqrt_price_x96
    s_start = s
    tick = sqrt_x96_to_tick(s)
    tick_start = tick
    L = _active_liquidity(tick, positions)

    remaining = amount_in
    total_out = 0
    segments: list[Segment] = []
    stop_reason = STOP_FILLED
    stop_tick: Optional[int] = None

    while remaining > 0:
        if len(segments) >= MAX_SWAP_STEPS:
            raise RuntimeError("报价跨越区间数超过安全上限")

        # 价格已在 MAX_TICK 边界：上方不存在任何 tick 区间，直接到网格尽头。
        # 先于空区间判断：边界上的仓位对上行交换无效（区间是 [lower, upper)），
        # 但停止原因仍是 edge（价格一步都不能移动），而非 gap。
        if s == MAX_SQRT_X96:
            stop_reason = STOP_INCOMPLETE_EDGE
            stop_tick = MAX_TICK
            break

        if L == 0:
            # 空区间：立即停止，剩余输入原样返回（本段尚未消耗任何输入）
            stop_reason = STOP_INCOMPLETE_GAP
            stop_tick = tick
            break

        # 下一个严格高于当前 tick 的事件边界，网格尽头为 MAX_TICK
        greater = [t for t in events if t > tick]
        target_t = min(greater) if greater else MAX_TICK
        is_edge = target_t == MAX_TICK and target_t not in greater

        is_limit = False
        if limit_tick is not None and tick_sqrt_x96(limit_tick) <= tick_sqrt_x96(target_t):
            target_t = limit_tick
            is_limit = True
            is_edge = False

        s1 = tick_sqrt_x96(target_t)
        cap = _principal_cap_up(L, s, s1)
        gross_cap = cap + fee_on(cap, pool.fee_ppm)

        if remaining < gross_cap:
            # 段内部分成交，输入耗尽
            fee = fee_on(remaining, pool.fee_ppm)
            principal = remaining - fee
            s2 = _partial_s_up(L, s, principal)
            out = _out_up(L, s, s2)
            segments.append(
                _segment(len(segments), tick, target_t, L, s, s2, remaining, fee, principal, out, "partial")
            )
            total_out += out
            remaining = 0
            s = s2
            stop_reason = STOP_FILLED
            break

        # 恰好走到 tick 边界：只消耗精确的过界容量（ceil），剩余留给下一段
        fee = fee_on(cap, pool.fee_ppm)
        out = _out_up(L, s, s1)
        segments.append(
            _segment(
                len(segments), tick, target_t, L, s, s1, gross_cap, fee, cap, out,
                "stop_limit" if is_limit else "stop_edge" if is_edge else "cross",
                crossed_tick=None if (is_limit or is_edge) else target_t,
            )
        )
        total_out += out
        remaining -= gross_cap
        s = s1
        tick = target_t
        if is_limit:
            stop_reason = STOP_INCOMPLETE_LIMIT
            stop_tick = target_t
            break
        if is_edge:
            stop_reason = STOP_INCOMPLETE_EDGE
            stop_tick = target_t
            break
        L += deltas.get(target_t, 0)

    result = QuoteResult(
        direction="zeroForOne=false (token1->token0)",
        token_in=pool.token1,
        token_out=pool.token0,
        amount_in=amount_in,
        amount_in_unfilled=remaining,
        amount_out_total=total_out,
        fee_total=amount_in - remaining - sum(int(x.principal_in) for x in segments),
        stop_reason=stop_reason,
        stop_tick=stop_tick,
        sqrt_price_start_x96=s_start,
        sqrt_price_end_x96=s,
        tick_start=tick_start,
        tick_end=sqrt_x96_to_tick(s),
        segments=segments,
    )
    _assert_reconciled(result)
    return result


def _swap_down(pool: Pool, amount_in: int, limit_tick: Optional[int]) -> QuoteResult:
    """token0 -> token1，价格沿 tick 网格下行。"""
    _check_limit_down(limit_tick, pool.sqrt_price_x96)
    positions = pool.positions
    deltas = _delta_map(positions)
    events = sorted(deltas)

    s = pool.sqrt_price_x96
    s_start = s
    tick = sqrt_x96_to_tick(s)
    tick_start = tick
    L = _active_liquidity(tick, positions)

    remaining = amount_in
    total_out = 0
    segments: list[Segment] = []
    stop_reason = STOP_FILLED
    stop_tick: Optional[int] = None

    while remaining > 0:
        if len(segments) >= MAX_SWAP_STEPS:
            raise RuntimeError("报价跨越区间数超过安全上限")

        # 价格已在 MIN_TICK 边界：下方不存在任何 tick 区间，直接到网格尽头。
        # 先于空区间判断：以 MIN_TICK 为下界的仓位在边界上虽然活跃，
        # 但下行交换一步都不能移动，停止原因是 edge 而非 gap。
        if s == MIN_SQRT_X96:
            stop_reason = STOP_INCOMPLETE_EDGE
            stop_tick = MIN_TICK
            break

        if L == 0:
            stop_reason = STOP_INCOMPLETE_GAP
            stop_tick = tick
            break

        # 下一个不高于当前 tick 的事件边界（可能就在脚下），网格尽头为 MIN_TICK
        le = [t for t in events if t <= tick]
        target_t = max(le) if le else MIN_TICK
        is_edge = target_t == MIN_TICK and target_t not in le

        is_limit = False
        if limit_tick is not None and tick_sqrt_x96(limit_tick) >= tick_sqrt_x96(target_t):
            target_t = limit_tick
            is_limit = True
            is_edge = False

        s1 = tick_sqrt_x96(target_t)

        if s1 == s:
            # 价格正落在事件边界上：零长度跨界，先应用流动性变化再继续。
            # 下行穿过边界 t 后，状态 tick 变为 t-1；下界开仓失效、上界平仓生效，
            # 净变化为 -deltas[t]。
            L -= deltas.get(target_t, 0)
            tick = target_t - 1
            continue

        cap = _principal_cap_down(L, s, s1)
        gross_cap = cap + fee_on(cap, pool.fee_ppm)

        if remaining < gross_cap:
            fee = fee_on(remaining, pool.fee_ppm)
            principal = remaining - fee
            s2 = _partial_s_down(L, s, principal)
            out = _out_down(L, s, s2)
            segments.append(
                _segment(len(segments), target_t, tick + 1, L, s, s2, remaining, fee, principal, out, "partial")
            )
            total_out += out
            remaining = 0
            s = s2
            stop_reason = STOP_FILLED
            break

        fee = fee_on(cap, pool.fee_ppm)
        out = _out_down(L, s, s1)
        segments.append(
            _segment(
                len(segments), target_t, tick + 1, L, s, s1, gross_cap, fee, cap, out,
                "stop_limit" if is_limit else "stop_edge" if is_edge else "cross",
                crossed_tick=None if (is_limit or is_edge) else target_t,
            )
        )
        total_out += out
        remaining -= gross_cap
        s = s1
        if is_limit:
            stop_reason = STOP_INCOMPLETE_LIMIT
            stop_tick = target_t
            break
        if is_edge:
            stop_reason = STOP_INCOMPLETE_EDGE
            stop_tick = target_t
            break
        # 下行跨界：状态 tick 变为 target_t - 1
        L -= deltas.get(target_t, 0)
        tick = target_t - 1

    result = QuoteResult(
        direction="zeroForOne=true (token0->token1)",
        token_in=pool.token0,
        token_out=pool.token1,
        amount_in=amount_in,
        amount_in_unfilled=remaining,
        amount_out_total=total_out,
        fee_total=amount_in - remaining - sum(int(x.principal_in) for x in segments),
        stop_reason=stop_reason,
        stop_tick=stop_tick,
        sqrt_price_start_x96=s_start,
        sqrt_price_end_x96=s,
        tick_start=tick_start,
        tick_end=sqrt_x96_to_tick(s),
        segments=segments,
    )
    _assert_reconciled(result)
    return result


def _segment(
    index: int,
    tick_lo: int,
    tick_hi: int,
    L: int,
    s0: int,
    s1: int,
    gross: int,
    fee: int,
    principal: int,
    out: int,
    ended_by: str,
    crossed_tick: Optional[int] = None,
) -> Segment:
    return Segment(
        segment_index=index,
        tick_lo=tick_lo,
        tick_hi=tick_hi,
        liquidity=str(L),
        sqrt_price_start_x96=str(s0),
        sqrt_price_end_x96=str(s1),
        price_start=_price_str(s0),
        price_end=_price_str(s1),
        amount_in_gross=str(gross),
        fee=str(fee),
        principal_in=str(principal),
        amount_out=str(out),
        ended_by=ended_by,
        crossed_tick=crossed_tick,
    )


def _assert_reconciled(result: QuoteResult) -> None:
    principal_sum = sum(int(x.principal_in) for x in result.segments)
    fee_sum = sum(int(x.fee) for x in result.segments)
    gross_sum = sum(int(x.amount_in_gross) for x in result.segments)
    if gross_sum != principal_sum + fee_sum:
        raise AssertionError("段内 gross != principal + fee")
    if fee_sum != result.fee_total:
        raise AssertionError("费用合计不一致")
    if result.amount_in != fee_sum + principal_sum + result.amount_in_unfilled:
        raise AssertionError("输入对账失败：in != fees + principals + unfilled")
