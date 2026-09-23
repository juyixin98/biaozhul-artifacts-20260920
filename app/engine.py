"""回放引擎: 纯函数式、确定性、无 I/O。

全部金额/价格/因子均为 1e18 缩放整数 (见 :mod:`app.fixedpoint`)。

时间模型
========
* 所有时间戳为 Unix 秒整数。
* 事件按 ``(ts, seq, event_id)`` 规范化排序后依次回放;
  同 ``ts`` 的事件严格按 ``seq`` 升序处理 (``event_id`` 仅作最终兜底),
  因此乱序/迟到事件重算结果完全一致。
* 分段利率为半开区间: 自 ``effective_ts`` (含) 起生效, 直到下一段开始
  (不含)。首个分段之前利率为 0。
* 利率分段在回放前依据 *全部* RATE_SCHEDULE 事件预先构建 —— 这样一个
  迟到提交、但 ``effective_ts`` 较早的利率修订事件也会确定性地修正历史
  计息 (即版本化重算的核心)。同一 ``effective_ts`` 重复设定时以规范化
  顺序中最后的 (``(ts,seq)`` 最大的) 为准。
* 债务利息 (区间内单利, 年化利率, 365 天通用年, 每个区间边界并入本金):
  ``interest = debt * annual_rate * dt // (WAD * 31536000)``
  只在区间结束处做一次向下取整, 不预先把年化利率截断成每秒利率。

清算规则
========
* 健康度 ``HF = collateral * price * threshold / (debt * WAD^2)``。
  严格 ``HF < 1`` (跨乘整数比较) 才允许清算; ``HF == 1`` 不清算。
* 预言机价格年龄严格大于 60 秒视为陈旧, 陈旧 (或无价格) 时拒绝清算。
* 单次清算请求偿还额必须 ``0 < repay <= floor(debt / 2)`` (50% 上限,
  超额请求直接拒绝而非截断)。
* 奖励: 偿还 ``repay`` 债务应得抵押
  ``seize = repay * bonus / price``, ``bonus = 1.08`` (8% 奖励)。
  抵押不足时按比例足额成交:
  ``repay = collateral * price / bonus`` (向下取整), ``seize = collateral``;
  向下取整后为 0 则拒绝 (DUST), 协议不承担坏账缺口。
"""
from __future__ import annotations

import hashlib
from bisect import bisect_right
from collections.abc import Iterable, Sequence

from .config import settings
from .crypto import canonical_json
from .fixedpoint import WAD

ACTIONS = {"OPEN", "ORACLE", "RATE_SCHEDULE", "REPAY", "LIQUIDATE"}


# ---------------------------------------------------------------- 事件校验


def _is_nonneg_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x >= 0


def _is_pos_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x > 0


def validate_event(ev: dict) -> str | None:
    """返回 None 表示合法; 否则返回机器可读的拒绝原因。"""
    if not isinstance(ev, dict):
        return "MALFORMED"
    event_id = ev.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        return "MALFORMED_EVENT_ID"
    if not _is_nonneg_int(ev.get("ts")):
        return "MALFORMED_TS"
    if not _is_nonneg_int(ev.get("seq")):
        return "MALFORMED_SEQ"
    action = ev.get("action")
    if action not in ACTIONS:
        return "UNKNOWN_ACTION"
    p = ev.get("payload")
    if not isinstance(p, dict):
        return "MALFORMED_PAYLOAD"

    if action == "OPEN":
        if not isinstance(p.get("position_id"), str) or not p["position_id"]:
            return "MALFORMED_POSITION_ID"
        if not isinstance(p.get("asset"), str) or not p["asset"]:
            return "MALFORMED_ASSET"
        if not _is_pos_int(p.get("collateral")):
            return "MALFORMED_COLLATERAL"
        if not _is_pos_int(p.get("debt")):
            return "MALFORMED_DEBT"
        thr = p.get("liquidation_threshold")
        if not _is_pos_int(thr) or thr > WAD:
            return "MALFORMED_THRESHOLD"
    elif action == "ORACLE":
        if not isinstance(p.get("asset"), str) or not p["asset"]:
            return "MALFORMED_ASSET"
        if not _is_pos_int(p.get("price")):
            return "MALFORMED_PRICE"
    elif action == "RATE_SCHEDULE":
        if not _is_nonneg_int(p.get("effective_ts")):
            return "MALFORMED_EFFECTIVE_TS"
        if not _is_nonneg_int(p.get("annual_rate_wad")):
            return "MALFORMED_ANNUAL_RATE"
    elif action in ("REPAY", "LIQUIDATE"):
        if not isinstance(p.get("position_id"), str) or not p["position_id"]:
            return "MALFORMED_POSITION_ID"
        key = "amount" if action == "REPAY" else "repay"
        if not _is_pos_int(p.get(key)):
            return "MALFORMED_AMOUNT"
    return None


def canonical_order(events: Iterable[dict]) -> list[dict]:
    """规范化排序: (ts, seq, event_id) 升序, 保证同时间事件稳定确定。"""
    return sorted(events, key=lambda e: (e["ts"], e["seq"], e["event_id"]))


def _build_schedule(ordered_events: Sequence[dict]) -> tuple[list[int], list[int]]:
    """从全部 RATE_SCHEDULE 事件预构建升序分段表。

    返回 (effective_ts 列表, 年化利率列表, 均为 WAD 缩放整数); 同一
    effective_ts 以规范化顺序中最后出现的 (seq 更大) 为准。
    """
    table: dict[int, int] = {}
    for ev in ordered_events:
        if ev.get("action") != "RATE_SCHEDULE":
            continue
        if validate_event(ev) is not None:
            continue
        p = ev["payload"]
        table[p["effective_ts"]] = p["annual_rate_wad"]
    ts_list = sorted(table)
    return ts_list, [table[t] for t in ts_list]


# ---------------------------------------------------------------- 回放主体


class _Replay:
    def __init__(self, sched_ts: list[int], sched_rate: list[int]) -> None:
        self.positions: dict[str, dict] = {}
        self.prices: dict[str, tuple[int, int]] = {}  # asset -> (price, price_ts)
        # 分段: 并行列表, 按 effective_ts 升序; 利率为年化 WAD 值
        self.sched_ts = sched_ts
        self.sched_rate = sched_rate
        self.actions: list[dict] = []
        self.liquidations: list[dict] = []
        self.last_ts = 0

    # ---- 分段利率 ----

    def _annual_rate_at(self, ts: int) -> int:
        i = bisect_right(self.sched_ts, ts) - 1
        return self.sched_rate[i] if i >= 0 else 0

    def accrue(self, pos: dict, t: int) -> None:
        """把 pos 的债务从 checkpoint 累计到 t (半开区间, 分段单利)。"""
        cur = pos["checkpoint"]
        debt = pos["debt"]
        while cur < t:
            annual = self._annual_rate_at(cur)
            i = bisect_right(self.sched_ts, cur)  # 第一个严格晚于 cur 的分段点
            nxt = self.sched_ts[i] if i < len(self.sched_ts) else t
            end = nxt if nxt < t else t
            dt = end - cur
            if dt > 0:
                debt += (
                    debt * annual * dt
                    // (WAD * settings.seconds_per_year)
                )
            cur = end
        pos["debt"] = debt
        pos["checkpoint"] = t

    def _log(self, ev: dict, status: str, reason: str | None = None, **detail) -> None:
        rec: dict = {
            "event_id": ev["event_id"],
            "ts": ev["ts"],
            "seq": ev["seq"],
            "action": ev["action"],
            "status": status,
        }
        if reason is not None:
            rec["reason"] = reason
        if detail:
            rec["detail"] = detail
        self.actions.append(rec)

    # ---- 各动作 ----

    def apply(self, ev: dict) -> None:
        action = ev["action"]
        p = ev["payload"]
        ts = ev["ts"]
        self.last_ts = max(self.last_ts, ts)

        if action == "RATE_SCHEDULE":
            # 分段表已在回放前预构建; 这里只保留审计轨迹。
            self._log(
                ev,
                "applied",
                annual_rate_wad=p["annual_rate_wad"],
                effective_ts=p["effective_ts"],
            )
            return

        if action == "OPEN":
            if p["position_id"] in self.positions:
                self._log(ev, "rejected", "POSITION_EXISTS")
                return
            self.positions[p["position_id"]] = {
                "position_id": p["position_id"],
                "asset": p["asset"],
                "collateral": p["collateral"],
                "debt": p["debt"],
                "liquidation_threshold": p["liquidation_threshold"],
                "opened_at": ts,
                "checkpoint": ts,
            }
            self._log(ev, "applied")
            return

        if action == "ORACLE":
            old = self.prices.get(p["asset"])
            self.prices[p["asset"]] = (p["price"], ts)
            self._log(ev, "applied", detail={"replaced": old is not None})
            return

        pos = self.positions.get(p["position_id"])
        if pos is None:
            self._log(ev, "rejected", "NO_POSITION")
            return
        self.accrue(pos, ts)

        if action == "REPAY":
            amount = p["amount"]
            if amount > pos["debt"]:
                self._log(ev, "rejected", "OVERPAY", debt=pos["debt"])
                return
            pos["debt"] -= amount
            self._log(ev, "applied", remaining_debt=pos["debt"])
            return

        if action == "LIQUIDATE":
            self._liquidate(ev, pos)

    def _liquidate(self, ev: dict, pos: dict) -> None:
        p = ev["payload"]
        ts = ev["ts"]
        requested = p["repay"]
        debt = pos["debt"]
        collateral = pos["collateral"]
        asset = pos["asset"]

        quote = self.prices.get(asset)
        if quote is None:
            self._log(ev, "rejected", "NO_PRICE")
            return
        price, price_ts = quote
        if ts - price_ts > settings.price_staleness_seconds:
            self._log(
                ev,
                "rejected",
                "STALE_PRICE",
                price_age=ts - price_ts,
                price_ts=price_ts,
            )
            return

        # 严格 HF < 1: 用跨乘整数比较, 保证恰好 1.0 不被清算。
        num = collateral * price * pos["liquidation_threshold"]
        den = debt * WAD * WAD
        if debt == 0 or num >= den:
            self._log(
                ev,
                "rejected",
                "HEALTHY",
                hf_num=num,
                hf_den=den,
                debt=debt,
            )
            return

        cap = debt // settings.max_repay_fraction_den  # 50% 上限
        if requested > cap:
            self._log(ev, "rejected", "EXCEEDS_50_PERCENT", cap=cap, debt=debt)
            return

        bonus = (
            WAD
            * settings.liquidation_bonus_num
            // settings.liquidation_bonus_den
        )
        repay = requested
        # 没收抵押 = repay * bonus / price (单次向下取整)
        full_seize = (repay * bonus) // price
        partial = collateral < full_seize
        if partial:
            # 抵押不足: 按可用抵押与 8% 奖励比例足额成交
            # repay = collateral * price / bonus (向下取整)
            repay = (collateral * price) // bonus
            if repay == 0:
                self._log(ev, "rejected", "DUST", collateral=collateral, price=price)
                return
            seized = collateral
        else:
            seized = full_seize

        pos["debt"] -= repay
        pos["collateral"] -= seized
        rec = {
            "event_id": ev["event_id"],
            "ts": ts,
            "seq": ev["seq"],
            "position_id": pos["position_id"],
            "requested_repay": requested,
            "repay": repay,
            "seized_collateral": seized,
            "price": price,
            "bonus_factor": bonus,
            "partial_fill": partial,
            "remaining_debt": pos["debt"],
            "remaining_collateral": pos["collateral"],
        }
        self.liquidations.append(rec)
        self._log(ev, "applied", repay=repay, seized_collateral=seized)


# ---------------------------------------------------------------- 报告


def _health(pos: dict, price: int | None) -> dict:
    debt = pos["debt"]
    out: dict = {
        "debt": debt,
        "collateral": pos["collateral"],
        "liquidation_threshold": pos["liquidation_threshold"],
        "price": price,
    }
    if price is None:
        out.update({"liquidatable": None, "reason": "NO_PRICE"})
        return out
    num = pos["collateral"] * price * pos["liquidation_threshold"]
    den = debt * WAD * WAD
    out.update({"hf_num": num, "hf_den": den})
    if debt == 0:
        # 无债务: 视为健康 (无分母可清算)
        out.update({"hf_wad": None, "healthy": True, "liquidatable": False})
    else:
        # den > 0; num 可以为 0 (抵押为零) -> HF=0, 可清算
        out.update(
            {
                "hf_wad": (num * WAD) // den,
                "healthy": num >= den,
                "liquidatable": num < den,
            }
        )
    return out


def build_report(events: Sequence[dict], as_of: int | None = None) -> dict:
    """对事件集合做一次完整确定性回放并生成版本报告。

    不做任何签名校验 (那是仓储/服务层的职责)。迟到/乱序事件只需一并
    传入: 规范化排序 + 分段预构建保证结果只取决于事件集合本身。
    """
    ordered = canonical_order(events)
    sched_ts, sched_rate = _build_schedule(ordered)
    r = _Replay(sched_ts, sched_rate)

    malformed: list[dict] = []
    for ev in ordered:
        reason = validate_event(ev)
        if reason is not None:
            malformed.append(
                {
                    "event_id": ev.get("event_id")
                    if isinstance(ev.get("event_id"), str)
                    else "",
                    "ts": ev.get("ts") if _is_nonneg_int(ev.get("ts")) else 0,
                    "seq": ev.get("seq") if _is_nonneg_int(ev.get("seq")) else 0,
                    "action": ev.get("action"),
                    "status": "rejected",
                    "reason": reason,
                }
            )
            continue
        if as_of is not None and ev["ts"] > as_of:
            break
        r.apply(ev)

    if as_of is None:
        as_of = r.last_ts

    # 报告快照: 所有仓位统一计息到 as_of。
    for pos in r.positions.values():
        r.accrue(pos, as_of)

    prices_out: dict[str, dict] = {}
    for asset, (price, pts) in sorted(r.prices.items()):
        age = as_of - pts
        prices_out[asset] = {
            "price": price,
            "price_ts": pts,
            "age_seconds": age,
            "stale": age > settings.price_staleness_seconds,
        }

    positions_out = []
    for pid in sorted(r.positions):
        pos = r.positions[pid]
        quote = r.prices.get(pos["asset"])
        price = quote[0] if quote is not None else None
        positions_out.append(
            {
                "position_id": pid,
                "asset": pos["asset"],
                "opened_at": pos["opened_at"],
                "collateral": pos["collateral"],
                "debt": pos["debt"],
                "health": _health(pos, price),
            }
        )

    body = {
        "schema": "liqreplay-report/v1",
        "as_of": as_of,
        "params": {
            "price_staleness_seconds": settings.price_staleness_seconds,
            "max_repay_fraction": "1/2",
            "liquidation_bonus": (
                f"{settings.liquidation_bonus_num}/{settings.liquidation_bonus_den}"
            ),
            "seconds_per_year": settings.seconds_per_year,
        },
        "rate_schedule": [
            {"effective_ts": t, "annual_rate_wad": rate}
            for t, rate in zip(r.sched_ts, r.sched_rate)
        ],
        "prices": prices_out,
        "positions": positions_out,
        "liquidations": r.liquidations,
        "actions": malformed + r.actions,
        "event_count": len(ordered),
    }
    body["hash"] = hashlib.sha256(canonical_json(_without_hash(body))).hexdigest()
    return body


def _without_hash(body: dict) -> dict:
    return {k: v for k, v in body.items() if k != "hash"}


def report_hash(body: dict) -> str:
    return body["hash"]
