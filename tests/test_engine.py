"""引擎验收测试: 直接构造无签名事件, 验证计算与协议规则。"""
from __future__ import annotations

from app.engine import build_report, canonical_order
from app.fixedpoint import WAD

T = 1_700_000_000
ETH = "ETH"


def ev(event_id, ts, seq, action, payload):
    return {
        "event_id": event_id,
        "ts": ts,
        "seq": seq,
        "action": action,
        "payload": payload,
    }


def rate(ts_eff, annual):
    return ev(f"rate-{ts_eff}-{annual}", ts_eff, 0, "RATE_SCHEDULE",
              {"effective_ts": ts_eff, "annual_rate_wad": annual})


def open_pos(pid="A", collateral=10 * WAD, debt=10_000 * WAD, threshold=WAD,
             ts=T, asset=ETH, eid=None):
    return ev(eid or f"open-{pid}", ts, 0, "OPEN", {
        "position_id": pid, "asset": asset, "collateral": collateral,
        "debt": debt, "liquidation_threshold": threshold,
    })


def price(p, ts, eid=None, seq=0):
    return ev(eid or f"px-{ts}-{p}", ts, seq, "ORACLE",
              {"asset": ETH, "price": p})


def liq(eid, ts, pid="A", repay=10**18, seq=0):
    return ev(eid, ts, seq, "LIQUIDATE", {"position_id": pid, "repay": repay})


def repay(eid, ts, amount, pid="A", seq=0):
    return ev(eid, ts, seq, "REPAY", {"position_id": pid, "amount": amount})


def pos(report, pid="A"):
    return next(p for p in report["positions"] if p["position_id"] == pid)


def action(report, eid):
    return next(a for a in report["actions"] if a["event_id"] == eid)


# ---------------------------------------------------------------- 利率


def test_interest_half_year_exact():
    # 年化 10%, 半年: 10000 -> 10500 精确无取整误差
    events = [
        rate(T, 10**17),
        open_pos(),
        price(2_000 * WAD, T),
    ]
    r = build_report(events, as_of=T + 365 * 24 * 3600 // 2)
    assert pos(r)["debt"] == 10_500 * WAD


def test_rate_switch_boundary_is_half_open():
    # 0-3600s 年化 10%, 3600s 起年化 20%。
    # t=T+3600 的快照只含第一段利息, 切换时刻本身使用新利率(半开)。
    events = [
        rate(T, 10**17),
        rate(T + 3600, 2 * 10**17),
        open_pos(),
        price(2_000 * WAD, T),
    ]
    year = 365 * 24 * 3600
    at_boundary = build_report(events, as_of=T + 3600)
    expected = 10_000 * WAD + (10_000 * WAD * 10**17 * 3600) // (WAD * year)
    assert pos(at_boundary)["debt"] == expected
    assert expected != 10_000 * WAD  # 确实有利息

    one_second_after = build_report(events, as_of=T + 3601)
    expected2 = expected + (expected * (2 * 10**17) * 1) // (WAD * year)
    assert pos(one_second_after)["debt"] == expected2


def test_rate_before_first_segment_is_zero():
    # RATE_SCHEDULE 在 OPEN 之后到达, 但 effective_ts 早于 OPEN:
    # 预构建分段表, OPEN 之前利率为 0, 自生效时刻起 10%。
    events = [
        open_pos(ts=T + 100, eid="open-late"),
        price(2_000 * WAD, T + 100, eid="px-late"),
        rate(T, 10**17),
    ]
    year = 365 * 24 * 3600
    r = build_report(events, as_of=T + 100 + 3600)
    debt = pos(r)["debt"]
    # 前 100 秒无利率, 之后 3600 秒年化 10%
    interest = (10_000 * WAD * 10**17 * 3600) // (WAD * year)
    assert debt == 10_000 * WAD + interest


def test_late_rate_schedule_event_rewrites_history():
    # 同一事件集合, 只是其中一个分段是"迟到"的 -> 结果只由集合决定。
    base = [
        rate(T, 10**17),
        open_pos(),
        price(2_000 * WAD, T),
    ]
    r1 = build_report(base, as_of=T + 7200)
    with_late = base + [rate(T, 5 * 10**17)]  # 同刻修订为 50%
    r2 = build_report(with_late, as_of=T + 7200)
    assert pos(r2)["debt"] > pos(r1)["debt"]
    assert r1["hash"] != r2["hash"]


# ---------------------------------------------------------------- 健康度


def test_health_exactly_one_not_liquidatable():
    # threshold=1.0, collateral=1 ETH, price=1000, debt=1000 -> HF 恰好 1
    events = [
        rate(T, 0),
        open_pos(collateral=WAD, debt=1_000 * WAD, threshold=WAD),
        price(1_000 * WAD, T, eid="px"),
        liq("liq-hf1", T + 10, repay=1),
    ]
    r = build_report(events, as_of=T + 10)
    assert action(r, "liq-hf1")["status"] == "rejected"
    assert action(r, "liq-hf1")["reason"] == "HEALTHY"
    h = pos(r)["health"]
    assert h["hf_num"] == h["hf_den"]
    assert h["liquidatable"] is False
    assert h["healthy"] is True


def test_health_just_below_one_liquidatable():
    # 价格 999 -> HF = 0.999 < 1
    events = [
        rate(T, 0),
        open_pos(collateral=WAD, debt=1_000 * WAD, threshold=WAD),
        price(999 * WAD, T, eid="px"),
        liq("liq-go", T + 10, repay=1),
    ]
    r = build_report(events, as_of=T + 10)
    assert action(r, "liq-go")["status"] == "applied"


# ---------------------------------------------------------------- 清算规则


def test_repay_cap_is_50_percent_and_strict():
    events = [
        rate(T, 0),
        open_pos(),
        price(900 * WAD, T, eid="px"),
        liq("liq-over", T + 10, repay=5_001 * WAD),  # > 50%
        liq("liq-eq", T + 11, repay=5_000 * WAD),  # 恰好 50%
    ]
    r = build_report(events, as_of=T + 11)
    assert action(r, "liq-over")["reason"] == "EXCEEDS_50_PERCENT"
    assert action(r, "liq-eq")["status"] == "applied"
    assert pos(r)["debt"] == 5_000 * WAD


def test_repeated_liquidation_same_timestamp_each_capped_at_50pct():
    # 同一时间戳两条清算, seq 决定顺序; 第二条以剩余债务的 50% 为上限。
    events = [
        rate(T, 0),
        open_pos(),
        price(900 * WAD, T, eid="px"),
        liq("liq-1", T + 10, repay=5_000 * WAD, seq=0),
        liq("liq-2", T + 10, repay=2_500 * WAD, seq=1),
        liq("liq-3", T + 10, repay=2_500 * WAD, seq=2),  # 剩余 2500, 上限 1250
    ]
    r = build_report(events, as_of=T + 10)
    assert action(r, "liq-1")["status"] == "applied"
    assert action(r, "liq-2")["status"] == "applied"
    assert action(r, "liq-3")["reason"] == "EXCEEDS_50_PERCENT"
    assert len(r["liquidations"]) == 2
    assert pos(r)["debt"] == 2_500 * WAD


def test_canonical_order_is_total_and_stable():
    # 同一 event_id 的重复事件在规范化排序中相邻; (ts, seq, id) 为全序。
    l = liq("liq-dup", T + 10, repay=1_000 * WAD)
    events = [
        rate(T, 0),
        open_pos(),
        price(900 * WAD, T, eid="px"),
        l,
        dict(l),  # 完全重复
    ]
    order = canonical_order(events)
    ids = [e["event_id"] for e in order]
    assert ids[-2] == ids[-1] == "liq-dup"
    # 颠倒插入顺序不改变规范化结果
    assert canonical_order(events) == canonical_order(list(reversed(events)))


def test_liquidation_bonus_seizes_extra_collateral():
    # threshold 0.8 + 价格 1000: HF=0.8 < 1; 清算价 1000。
    # 偿还 1000, bonus=1.08 -> 没收 1.08 ETH (单次向下取整)
    events = [
        rate(T, 0),
        open_pos(collateral=10 * WAD, debt=10_000 * WAD, threshold=8 * 10**17),
        price(1_000 * WAD, T, eid="px"),
        liq("liq-bonus", T + 2, repay=1_000 * WAD),
    ]
    r = build_report(events, as_of=T + 2)
    liq_rec = r["liquidations"][0]
    assert liq_rec["repay"] == 1_000 * WAD
    # seize = repay * 1.08 / price = 1000 * 1.08 / 1000 = 1.08 ETH
    assert liq_rec["seized_collateral"] == (1_000 * WAD * 108 // 100) // 1_000
    assert liq_rec["seized_collateral"] == 108 * 10**16
    assert liq_rec["partial_fill"] is False


def test_insufficient_collateral_partial_fill_formula():
    # 抵押 0.001 ETH, price=900: 全额应得 > 抵押 -> 部分成交
    # repay = collateral*price/1.08 = 0.001*900/1.08 = 0.8333...
    events = [
        rate(T, 0),
        open_pos(pid="B", collateral=10**15, debt=10 * WAD, threshold=WAD),
        price(900 * WAD, T, eid="px"),
        liq("liq-partial", T + 5, pid="B", repay=5 * WAD),
    ]
    r = build_report(events, as_of=T + 5)
    rec = r["liquidations"][0]
    assert rec["partial_fill"] is True
    assert rec["seized_collateral"] == 10**15
    expected_repay = (10**15 * 900 * WAD) // (108 * WAD // 100)
    assert rec["repay"] == expected_repay
    assert rec["repay"] < 5 * WAD
    # 抵押被全部收走: 债务仍为正, HF=0 且仍可清算 (而不是被当成无分母)
    pb = pos(r, "B")
    assert pb["collateral"] == 0
    assert pb["health"]["hf_num"] == 0
    assert pb["health"]["hf_wad"] == 0
    assert pb["health"]["liquidatable"] is True
    assert pb["health"]["healthy"] is False


# ---------------------------------------------------------------- 陈旧价格


def test_stale_price_older_than_60s_pauses_liquidation():
    events = [
        rate(T, 0),
        open_pos(),
        price(900 * WAD, T, eid="px"),
        liq("liq-60", T + 60, repay=1_000 * WAD),   # 恰好 60s: 仍新鲜
        liq("liq-61", T + 61, repay=1_000 * WAD),   # 61s: 陈旧
    ]
    r = build_report(events, as_of=T + 61)
    assert action(r, "liq-60")["status"] == "applied"
    assert action(r, "liq-61")["reason"] == "STALE_PRICE"
    assert action(r, "liq-61")["detail"]["price_age"] == 61
    # 陈旧只暂停清算, 利息照常累计
    assert pos(r)["debt"] == 9_000 * WAD


# ---------------------------------------------------------------- 同时间排序


def test_same_timestamp_stable_order_regardless_of_insertion():
    # 同 ts: seq=0 ORACLE 900; seq=1 LIQUIDATE(原本健康)
    # 若 LIQUIDATE 先处理, 它只会看到 T-100 时 2000 的旧价而判 HEALTHY;
    # 规范化顺序 (ts,seq) 下新价先生效, 清算应成功。
    px_new = price(900 * WAD, T + 10, eid="px-new", seq=0)
    l = liq("liq-ordered", T + 10, repay=1_000 * WAD, seq=1)

    def build(tail):
        events = [
            rate(T, 0),
            open_pos(),
            price(2_000 * WAD, T - 100, eid="px-old"),
        ]
        events.extend(tail)
        return build_report(events, as_of=T + 10)

    r_forward = build([px_new, l])
    r_reversed = build([l, px_new])  # 插入顺序颠倒
    assert action(r_forward, "liq-ordered")["status"] == "applied"
    assert action(r_reversed, "liq-ordered")["status"] == "applied"
    assert r_forward["hash"] == r_reversed["hash"]
