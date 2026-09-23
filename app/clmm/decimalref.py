"""用 Decimal 的慢速独立参考实现。

用途：测试中与整数引擎交叉验证。它是**第二套独立推导**的实现：

* 连续价格直接以 ``Decimal`` 形式表示为 ``q = s / 2**96``；
* tick 边界连续价格直接由 ``(10001/10000) ** (t/2)`` 用 Decimal 求幂得到，
  不使用 pricing.tick_sqrt_x96 的整数 ceil 边界；
* 每段的容量、输出数量都用 Decimal 连续公式独立计算。

两种模式
--------

``grid=True``（默认）：参考实现也逐 tick 边界结算，并在边界上施加与引擎
相同方向的整数舍入。此时它应与引擎**完全一致**（逐段费用、本金、输出、
未成交输入全部相等），这是对引擎最严格的验证。

``grid=False``：连续 Decimal 模型——部分成交直接落在连续价格上，用于检验
整数引擎的结果在经济上紧邻连续模型（不高估输出，且不低估超过最小单位）。
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, getcontext
from typing import Optional

from .constants import (
    MAX_SWAP_STEPS,
    MAX_TICK,
    MIN_TICK,
    Q96_SCALE,
    STOP_FILLED,
    STOP_INCOMPLETE_EDGE,
    STOP_INCOMPLETE_GAP,
    STOP_INCOMPLETE_LIMIT,
)
from .engine import Pool, Position, _delta_map, fee_on

getcontext().prec = 100

_Q = Decimal(Q96_SCALE)
_BASE = Decimal(10001) / Decimal(10000)


def _ceil_int(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_CEILING))


def _floor_int(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_FLOOR))


_boundary_q_cache: dict[int, Decimal] = {}
_boundary_s_cache: dict[int, int] = {}


def boundary_q(tick: int) -> Decimal:
    """tick 边界的连续 sqrtPrice（q 尺度），Decimal 独立计算。"""
    v = _boundary_q_cache.get(tick)
    if v is None:
        v = _BASE ** (Decimal(tick) / 2)
        _boundary_q_cache[tick] = v
    return v


def boundary_s(tick: int) -> int:
    """tick 边界的整数 sqrtPriceX96，独立用 Decimal ROUND_CEILING 推导。

    与 pricing.tick_sqrt_x96（整数 isqrt 路线）是两套独立算法，
    测试中断言二者相等以交叉验证价格映射。
    """
    v = _boundary_s_cache.get(tick)
    if v is None:
        v = int((_Q * boundary_q(tick)).to_integral_value(rounding=ROUND_CEILING))
        _boundary_s_cache[tick] = v
    return v


@dataclass
class RefSegment:
    tick_lo: int
    tick_hi: int
    liquidity: int
    q_start: Decimal
    q_end: Decimal
    gross: int
    fee: int
    principal: int
    out: int
    ended_by: str
    crossed_tick: Optional[int] = None


@dataclass
class RefResult:
    segments: list[RefSegment]
    total_out: int
    unfilled: int
    stop_reason: str
    stop_tick: Optional[int]
    q_start: Decimal
    q_end: Decimal
    tick_start: int
    tick_end: int

    @property
    def fee_total(self) -> int:
        return sum(x.fee for x in self.segments)


def _tick_floor(q: Decimal) -> int:
    """连续 q 反查 tick（floor），Decimal 独立二分。"""
    lo, hi = MIN_TICK, MAX_TICK + 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if boundary_q(mid) <= q:
            lo = mid
        else:
            hi = mid
    return lo


def _active(tick: int, positions: tuple[Position, ...]) -> int:
    return sum(p.liquidity for p in positions if p.lower_tick <= tick < p.upper_tick)


def reference_swap(
    pool: Pool,
    zero_for_one: bool,
    amount_in: int,
    limit_tick: Optional[int] = None,
    grid: bool = True,
) -> RefResult:
    """Decimal 慢速参考报价。"""
    pool.validate()
    if not isinstance(amount_in, int) or amount_in <= 0:
        raise ValueError("amount_in 必须为正整数")
    if limit_tick is not None and not (MIN_TICK <= limit_tick <= MAX_TICK):
        raise ValueError("limit_tick 越界")

    s = pool.sqrt_price_x96            # 当前段起始整数 sqrtPriceX96
    q = Decimal(s) / _Q                # 连续价格
    if limit_tick is not None:
        bq0 = boundary_q(limit_tick)
        if zero_for_one and bq0 >= q:
            raise ValueError("limit_tick 必须位于价格下行方向")
        if not zero_for_one and bq0 <= q:
            raise ValueError("limit_tick 必须位于价格上行方向")
    q_start = q
    tick = _tick_floor(q)
    tick_start = tick
    L = _active(tick, pool.positions)
    deltas = _delta_map(pool.positions)
    events = sorted(deltas)

    remaining = amount_in
    total_out = 0
    segs: list[RefSegment] = []
    stop = STOP_FILLED
    stop_tick: Optional[int] = None

    while remaining > 0:
        if len(segs) >= MAX_SWAP_STEPS:
            raise RuntimeError("参考实现超过步数上限")
        # 网格尽头优先于空区间（边界上即使有活跃仓位也无法继续）
        edge_s = boundary_s(MAX_TICK) if not zero_for_one else boundary_s(MIN_TICK)
        if s == edge_s:
            stop = STOP_INCOMPLETE_EDGE
            stop_tick = MAX_TICK if not zero_for_one else MIN_TICK
            break
        if L == 0:
            stop = STOP_INCOMPLETE_GAP
            stop_tick = tick
            break

        if zero_for_one:
            le = [t for t in events if t <= tick]
            target_t = max(le) if le else MIN_TICK
            is_edge = target_t == MIN_TICK and target_t not in le
        else:
            greater = [t for t in events if t > tick]
            target_t = min(greater) if greater else MAX_TICK
            is_edge = target_t == MAX_TICK and target_t not in greater

        is_limit = False
        if limit_tick is not None:
            bq = boundary_q(limit_tick)
            if (zero_for_one and bq >= boundary_q(target_t)) or (
                not zero_for_one and bq <= boundary_q(target_t)
            ):
                target_t = limit_tick
                is_limit = True
                is_edge = False

        q1_cont = boundary_q(target_t)
        s1_int = boundary_s(target_t)

        if zero_for_one and grid and s == s1_int:
            # 价格正落在整数网格事件边界上：零长度跨界，仅更新流动性
            L -= deltas.get(target_t, 0)
            tick = target_t - 1
            continue
        if zero_for_one and not grid and q1_cont == q:
            L -= deltas.get(target_t, 0)
            tick = target_t - 1
            continue

        q1 = Decimal(s1_int) / _Q if grid else q1_cont

        # --- 段容量：网格模式基于吸附后的整数边界，ceil ---
        if zero_for_one:
            cap_exact = Decimal(L) * (q - q1)
        else:
            cap_exact = Decimal(L) * (q1 - q) / (q * q1)
        cap = _ceil_int(cap_exact)
        gross_cap = cap + fee_on(cap, pool.fee_ppm)

        if remaining < gross_cap:
            fee = fee_on(remaining, pool.fee_ppm)
            principal = remaining - fee
            if principal == 0:
                # 本金为 0（输入全部被 ceil 费用吃掉）时价格不移动。
                # 必须短路，否则 Decimal 的 L*q/L 会带入相对舍入误差。
                s2 = s
                q2_exact = q
            else:
                p = Decimal(principal)
                if zero_for_one:
                    q2_exact = q - p / Decimal(L)
                    # 引擎把段终价朝段内夹持（下行不越过真实终点）：ceil(Q*q2_exact)
                    s2 = (
                        int((_Q * q2_exact).to_integral_value(rounding=ROUND_CEILING))
                        if grid
                        else None
                    )
                else:
                    q2_exact = Decimal(L) * q / (Decimal(L) - p * q)
                    # 上行夹持：floor(Q*q2_exact)
                    s2 = (
                        int((_Q * q2_exact).to_integral_value(rounding=ROUND_FLOOR))
                        if grid
                        else None
                    )
            if grid:
                q2 = Decimal(s2) / _Q
                # 输出基于吸附后的整数网格价独立用 Decimal 复算后 floor。
                # q 空间下整数 token 数量公式恰好消去 2^96：
                # down out=L(q-q2)/(q*q2)，up out=L(q2-q)
                if zero_for_one:
                    out_exact = Decimal(L) * (q - q2) / (q * q2)
                else:
                    out_exact = Decimal(L) * (q2 - q)
            else:
                q2 = q2_exact
                if zero_for_one:
                    out_exact = Decimal(L) * (q - q2_exact) / (q * q2_exact)
                else:
                    out_exact = Decimal(L) * (q2_exact - q)
            out = max(0, _floor_int(out_exact))
            segs.append(
                RefSegment(
                    tick_lo=target_t if zero_for_one else tick,
                    tick_hi=tick + 1 if zero_for_one else target_t,
                    liquidity=L,
                    q_start=q,
                    q_end=q2,
                    gross=remaining,
                    fee=fee,
                    principal=principal,
                    out=out,
                    ended_by="partial",
                )
            )
            total_out += out
            remaining = 0
            q = q2
            if grid:
                s = s2
            break

        # --- 恰好走到 tick 边界（q 空间公式，2^96 已消去）---
        if zero_for_one:
            out_exact = Decimal(L) * (q - q1) / (q * q1)
        else:
            out_exact = Decimal(L) * (q1 - q)
        out = max(0, _floor_int(out_exact))
        segs.append(
            RefSegment(
                tick_lo=target_t if zero_for_one else tick,
                tick_hi=tick + 1 if zero_for_one else target_t,
                liquidity=L,
                q_start=q,
                q_end=q1,
                gross=gross_cap,
                fee=fee_on(cap, pool.fee_ppm),
                principal=cap,
                out=out,
                ended_by="stop_limit" if is_limit else "stop_edge" if is_edge else "cross",
                crossed_tick=None if (is_limit or is_edge) else target_t,
            )
        )
        total_out += out
        remaining -= gross_cap
        q = q1
        s = s1_int
        if is_limit:
            stop = STOP_INCOMPLETE_LIMIT
            stop_tick = target_t
            break
        if is_edge:
            stop = STOP_INCOMPLETE_EDGE
            stop_tick = target_t
            break
        if zero_for_one:
            L -= deltas.get(target_t, 0)
            tick = target_t - 1
        else:
            L += deltas.get(target_t, 0)
            tick = target_t

    return RefResult(
        segments=segs,
        total_out=total_out,
        unfilled=remaining,
        stop_reason=stop,
        stop_tick=stop_tick,
        q_start=q_start,
        q_end=q,
        tick_start=tick_start,
        tick_end=_tick_floor(q),
    )
