"""借贷清算回放引擎（纯函数式、无 I/O）。

输入：规范化事件列表（金额已是 WAD 整数），每个事件带：
    event_id / ts(整数秒) / type / payload / seq(入库顺序，从1开始)
输出：完整报告快照（持仓、清算记录、跳过记录、价格状态等）。

关键约定
========
时间边界
* 事件时间 ts 为整数秒。
* 利息区间为半开区间 [t0, t1)：在 t0 时刻已产生、到 t1 时刻之前，共 t1-t0 秒。
  在事件发生的同一秒内不重复计息（dt=0 时利息为 0），利率在切换点当秒立即适用。
* 稳定排序：ORDER BY (ts ASC, seq ASC)。同一秒事件按"入库先后"确定处理，
  结果可复现；清算与其他事件共享同一条全序流。

计息（固定分段利率，线性、不复利）
* 全局利率表由 rate_schedule 事件在其 ts 处生效，分段半开。
* 每个计息段（段内债务恒定，因为事件之间无操作）一次性计算：
  interest = floor(tr1 * r1 * dt / WAD) + floor(tr2 * r1 * m1 * dt / (WAD * m2))
  其中 tr1 = min(D, m1)，tr2 = max(0, D - m1)，r2 = r1 * m1 / m2（m2=0 无第二档）。
  floor 保证系统不会凭空多记债务。

健康度
* H = 抵押品总价值 * WAD / 债务（整数比，比较时交叉相乘，H < WAD 才可清算）。
* 抵押品价值 = Σ floor(余额_asset * price_asset / WAD)。
* H 恰好等于 1（总价值恰好等于债务）不可清算。

清算
* 单次最多偿还当前债务的 50%：repay = floor(D * 1/2)。
* 清算奖励：需要没收的抵押品名义价值 target = ceil(repay * bonus_num / bonus_den)。
* 抵押充足：按 asset 升序贪心没收（确定性，最多在非最后一档多收 ≤1 最小单位）。
* 抵押不足：没收全部抵押品，实际还贷 effective = floor(总价值 * bonus_den / bonus_num)，
  记录 shortfall_value = target - 总价值，债务只减免 effective。
* 价格陈旧：任何有余额的抵押资产价格年龄 > 60 秒 => 暂停清算（记 skipped）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .numerics import WAD, mul_div_ceil, mul_div_floor


@dataclass
class Position:
    position_id: str
    opened_at: int
    debt: int = 0
    debt_last_ts: int = 0
    collateral: dict[str, int] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "opened_at": self.opened_at,
            "debt": self.debt,
            "debt_last_accrued_ts": self.debt_last_ts,
            "collateral": dict(self.collateral),
        }


# ---- 工具 -----------------------------------------------------------------


def _is_nonneg_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x >= 0


def _skip(ev: dict, reason: str, **extra: Any) -> dict:
    row = {"event_id": ev["event_id"], "ts": ev["ts"], "type": ev["type"], "reason": reason}
    row.update(extra)
    return row


# ---- 回放 -----------------------------------------------------------------


def replay(
    events: list[dict],
    *,
    replay_version: int,
    seconds_per_year: int,
    price_staleness_seconds: int = 60,
    max_repay_num: int = 1,
    max_repay_den: int = 2,
) -> dict:
    """对全部已入库事件做一次确定性全量回放。

    events 元素至少含 event_id/ts/type/payload/seq；金额字段为 WAD int。
    """
    ordered = sorted(events, key=lambda e: (e["ts"], e["seq"]))

    positions: dict[str, Position] = {}
    # 利率/协议参数时间点：ts -> params dict（同秒重复设置以后到者为准，顺序确定）
    rate_points: dict[int, dict] = {}
    prices: dict[str, tuple[int, int]] = {}  # asset -> (price_wad, ts)
    liquidations: list[dict] = []
    skipped: list[dict] = []

    max_ts = max((e["ts"] for e in ordered), default=0)

    def latest_params(ts: int) -> dict | None:
        cur = None
        for pts in sorted(rate_points):
            if pts <= ts:
                cur = rate_points[pts]
            else:
                break
        return cur

    def accrue(p: Position, to_ts: int) -> None:
        """把 p 的债务利息从 debt_last_ts 累计到 to_ts（半开区间）。"""
        if to_ts < p.debt_last_ts:
            raise AssertionError("计息时间倒退")
        if p.debt == 0 or to_ts == p.debt_last_ts:
            p.debt_last_ts = to_ts
            return
        pts_sorted = sorted(rate_points)
        cursor = p.debt_last_ts
        while cursor < to_ts:
            # 找 <= cursor 的最新生效参数，以及其后的第一个切换点
            params = None
            next_switch = to_ts
            current_idx = -1
            for i, pts in enumerate(pts_sorted):
                if pts <= cursor:
                    params = rate_points[pts]
                    current_idx = i
                else:
                    break
            if current_idx + 1 < len(pts_sorted):
                nxt = pts_sorted[current_idx + 1]
                if cursor < nxt < next_switch:
                    next_switch = nxt
            # cursor 早于所有利率点时，推进到第一个切换点（期间无利率，不计息）
            if params is None and pts_sorted and pts_sorted[0] > cursor:
                next_switch = min(next_switch, pts_sorted[0])
            end = min(next_switch, to_ts)
            dt = end - cursor
            if dt > 0 and params is not None:
                rate = params["rate_per_second"]
                m1 = params["tier1_m1"]
                m2 = params["tier2_m2"]
                d = p.debt
                # 分段线性（非复利）：整段一次性 floor，结果只取决于 [cursor,end) 与段内恒定债务。
                # 事件之间债务不变，因此与"每一秒 floor"相比不会漏掉应计利息。
                tr1 = min(d, m1)
                interest = mul_div_floor(tr1 * rate * dt, 1, WAD)
                if m2 > 0 and d > m1:
                    # r2 = rate * m1 / m2（第二档折减利率）
                    tr2 = d - m1
                    interest += mul_div_floor(tr2 * rate * m1 * dt, 1, WAD * m2)
                p.debt = d + interest
            cursor = end
        p.debt_last_ts = to_ts

    for ev in ordered:
        etype = ev["type"]
        ts = ev["ts"]
        payload = ev.get("payload") or {}

        if etype == "rate_schedule":
            rate = payload.get("rate_per_second")
            m1 = payload.get("tier1_m1")
            m2 = payload.get("tier2_m2", 0)
            bnum = payload.get("liq_bonus_num")
            bden = payload.get("liq_bonus_den")
            if not all(_is_nonneg_int(x) for x in (rate, m1, m2)) or not isinstance(
                bnum, int
            ) or not isinstance(bden, int) or bden <= 0 or bnum < bden:
                skipped.append(_skip(ev, "invalid_rate_schedule"))
                continue
            rate_points[ts] = {
                "rate_per_second": rate,
                "tier1_m1": m1,
                "tier2_m2": m2,
                "liq_bonus_num": bnum,
                "liq_bonus_den": bden,
                "debt_symbol": payload.get("debt_symbol", "DEBT"),
            }
            continue

        if etype == "open_position":
            pid = payload.get("position_id")
            if not isinstance(pid, str) or not pid:
                skipped.append(_skip(ev, "invalid_position_id"))
                continue
            if pid in positions:
                skipped.append(_skip(ev, "duplicate_position"))
                continue
            positions[pid] = Position(position_id=pid, opened_at=ts, debt_last_ts=ts)
            continue

        # 以下事件都针对已存在持仓
        p = positions.get(payload.get("position_id", "")) if isinstance(
            payload.get("position_id"), str
        ) else None
        amount = payload.get("amount")

        if etype in ("deposit", "withdraw", "borrow", "repay", "liquidate") and p is None:
            skipped.append(_skip(ev, "unknown_position"))
            continue

        if etype == "deposit":
            if not _is_nonneg_int(amount) or amount == 0:
                skipped.append(_skip(ev, "invalid_amount"))
                continue
            asset = payload.get("asset")
            if not isinstance(asset, str) or not asset:
                skipped.append(_skip(ev, "invalid_asset"))
                continue
            p.collateral[asset] = p.collateral.get(asset, 0) + amount
            continue

        if etype == "withdraw":
            if not _is_nonneg_int(amount) or amount == 0:
                skipped.append(_skip(ev, "invalid_amount"))
                continue
            asset = payload.get("asset")
            bal = p.collateral.get(asset, 0) if isinstance(asset, str) else 0
            if amount > bal:
                skipped.append(_skip(ev, "insufficient_collateral", balance=bal))
                continue
            p.collateral[asset] = bal - amount
            continue

        if etype == "borrow":
            if latest_params(ts) is None:
                skipped.append(_skip(ev, "no_rate_schedule"))
                continue
            if not _is_nonneg_int(amount) or amount == 0:
                skipped.append(_skip(ev, "invalid_amount"))
                continue
            accrue(p, ts)
            p.debt += amount
            continue

        if etype == "repay":
            if not _is_nonneg_int(amount) or amount == 0:
                skipped.append(_skip(ev, "invalid_amount"))
                continue
            accrue(p, ts)
            if amount > p.debt:
                skipped.append(_skip(ev, "repay_exceeds_debt", debt=p.debt))
                continue
            p.debt -= amount
            continue

        if etype == "price_update":
            asset = payload.get("asset")
            price = payload.get("price")
            if not isinstance(asset, str) or not asset or not _is_nonneg_int(price) or price == 0:
                skipped.append(_skip(ev, "invalid_price"))
                continue
            prices[asset] = (price, ts)
            continue

        if etype == "liquidate":
            rec = _do_liquidate(
                ev=ev,
                p=p,
                ts=ts,
                prices=prices,
                params=latest_params(ts),
                accrue=accrue,
                staleness=price_staleness_seconds,
                max_repay_num=max_repay_num,
                max_repay_den=max_repay_den,
            )
            if rec["status"] == "executed":
                liquidations.append(rec)
            else:
                skipped.append(
                    _skip(ev, rec["reason"], **{k: v for k, v in rec.items() if k not in (
                        "status", "reason", "event_id", "ts", "type")})
                )
            continue

        skipped.append(_skip(ev, "unknown_event_type"))

    # 报告快照：全部持仓计息到最后一个事件时间
    for p in positions.values():
        accrue(p, max_ts)

    current_params = latest_params(max_ts)
    position_views = []
    paused_assets: list[dict] = []
    for pid in sorted(positions):
        p = positions[pid]
        view = _position_view(p, ts=max_ts, prices=prices, staleness=price_staleness_seconds)
        position_views.append(view)
        for a, age in view["price_ages"].items():
            if age is not None and age > price_staleness_seconds:
                paused_assets.append({"position_id": pid, "asset": a, "age_seconds": age})
            if age is None:
                paused_assets.append({"position_id": pid, "asset": a, "age_seconds": None})

    return {
        "replay_version": replay_version,
        "max_event_ts": max_ts,
        "seconds_per_year": seconds_per_year,
        "price_staleness_seconds": price_staleness_seconds,
        "current_params": current_params,
        "positions": position_views,
        "liquidations": liquidations,
        "skipped": skipped,
        "latest_prices": [
            {"asset": a, "price": px, "ts": pts, "age_seconds": max_ts - pts}
            for a, (px, pts) in sorted(prices.items())
        ],
        "liquidations_paused": len(paused_assets) > 0,
        "paused_assets": paused_assets,
        "processed_event_ids": [e["event_id"] for e in ordered],
    }


def _asset_value(balance: int, price: int) -> int:
    return mul_div_floor(balance, price, WAD)


def _position_view(p: Position, *, ts: int, prices: dict, staleness: int) -> dict:
    collateral_value = 0
    price_ages: dict[str, int | None] = {}
    missing: list[str] = []
    stale: list[dict] = []
    for asset in sorted(p.collateral):
        bal = p.collateral[asset]
        if bal == 0:
            continue
        entry = prices.get(asset)
        if entry is None:
            price_ages[asset] = None
            missing.append(asset)
            continue
        price, pts = entry
        age = ts - pts
        price_ages[asset] = age
        collateral_value += _asset_value(bal, price)
        if age > staleness:
            stale.append({"asset": asset, "age_seconds": age})
    eligible = (
        p.debt > 0
        and not missing
        and not stale
        and collateral_value < p.debt  # H < 1；等于 1 不满足
    )
    return {
        **p.snapshot(),
        "collateral_value": collateral_value,
        "health_ratio_wad": (
            mul_div_floor(collateral_value, WAD, p.debt) if p.debt > 0 else None
        ),
        "price_ages": price_ages,
        "missing_price_assets": missing,
        "stale_price_assets": stale,
        "liquidatable": eligible,
    }


def _do_liquidate(
    *,
    ev: dict,
    p: Position,
    ts: int,
    prices: dict[str, tuple[int, int]],
    params: dict | None,
    accrue,
    staleness: int,
    max_repay_num: int,
    max_repay_den: int,
) -> dict:
    """执行单笔清算；返回 executed 记录或带 reason 的拒绝记录。"""
    base = {
        "status": "skipped",
        "event_id": ev["event_id"],
        "ts": ts,
        "type": "liquidate",
        "position_id": p.position_id,
        "liquidator": (ev.get("payload") or {}).get("liquidator"),
    }
    if params is None:
        return {**base, "reason": "no_rate_schedule"}

    # 1) 价格完备性与陈旧度（只看有余额的抵押资产）
    priced: dict[str, tuple[int, int, int]] = {}  # asset -> (balance, price, age)
    missing: list[str] = []
    stale: list[dict] = []
    for asset, bal in p.collateral.items():
        if bal == 0:
            continue
        entry = prices.get(asset)
        if entry is None:
            missing.append(asset)
            continue
        price, pts = entry
        age = ts - pts
        if age > staleness:
            stale.append({"asset": asset, "age_seconds": age})
        priced[asset] = (bal, price, age)
    if missing:
        return {**base, "reason": "missing_price", "assets": sorted(missing)}
    if stale:
        return {**base, "reason": "stale_price", "assets": stale}

    # 2) 计息到清算时刻
    accrue(p, ts)

    # 3) 零债务 / 健康度
    debt_before = p.debt
    if debt_before == 0:
        return {**base, "reason": "zero_debt"}
    total_value = sum(_asset_value(bal, px) for bal, px, _ in priced.values())
    if total_value >= debt_before:
        return {
            **base,
            "reason": "healthy",
            "collateral_value": total_value,
            "debt": debt_before,
        }

    # 4) 单次最多偿还 50%（向下取整；不足 1 最小单位则跳过）
    repay = mul_div_floor(debt_before, max_repay_num, max_repay_den)
    if repay == 0:
        return {**base, "reason": "dust_debt", "debt": debt_before}

    bnum = params["liq_bonus_num"]
    bden = params["liq_bonus_den"]
    # 奖励：需要没收的抵押品名义价值（向上取整保证足额）
    target_value = mul_div_ceil(repay, bnum, bden)

    seized: dict[str, int] = {}
    seized_value = 0
    shortfall_value = 0
    effective_repay: int

    if total_value <= target_value:
        # 抵押不足：没收全部，实际还贷按奖励比例反算（floor），差额记录
        for asset, (bal, _px, _age) in priced.items():
            if bal:
                seized[asset] = bal
        seized_value = total_value
        effective_repay = mul_div_floor(total_value, bden, bnum)
        if effective_repay == 0:
            return {
                **base,
                "reason": "collateral_too_dusty",
                "collateral_value": total_value,
            }
        shortfall_value = target_value - total_value
    else:
        # 抵押充足：按 asset 升序贪心没收至 target_value
        effective_repay = repay
        remaining = target_value
        for asset in sorted(priced):
            if remaining <= 0:
                break
            bal, px, _age = priced[asset]
            units = min(bal, mul_div_ceil(remaining, WAD, px))
            if units <= 0:
                continue
            seized[asset] = units
            value = _asset_value(units, px)
            seized_value += value
            remaining -= value  # value 可能使 remaining 变负（最后一档 ≤1 单位过收）

    # 5) 落账
    for asset, units in seized.items():
        p.collateral[asset] -= units
        if p.collateral[asset] == 0:
            del p.collateral[asset]
    p.debt = debt_before - effective_repay

    return {
        **base,
        "status": "executed",
        "debt_before": debt_before,
        "debt_after": p.debt,
        "max_repay": repay,
        "repaid": effective_repay,
        "bonus_num": bnum,
        "bonus_den": bden,
        "target_seize_value": target_value,
        "seized_value": seized_value,
        "seized_collateral": seized,
        "shortfall_value": shortfall_value,
        "collateral_value_before": total_value,
        "collateral_value_after": total_value - seized_value,
    }
