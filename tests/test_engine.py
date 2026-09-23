"""协议引擎测试：分段利率切换、时间边界、健康度恰好 1、重复清算、
抵押不足、陈旧价格、同时间事件稳定排序。
"""
from __future__ import annotations


from app.engine import replay
from app.numerics import WAD

RATE_1 = 1_000_000_000  # 1e-9 / 秒
RATE_2 = 2_000_000_000  # 2e-9 / 秒


def ev(event_id, ts, etype, payload, seq=None):
    """构造已规范化事件（金额为 WAD int）。seq 由计数器自动填充。"""
    e = {"event_id": event_id, "ts": ts, "type": etype, "payload": payload}
    e["_seq"] = seq
    return e


def run(events, **kw):
    out = []
    for i, e in enumerate(events, start=1):
        d = dict(e)
        d["seq"] = d.pop("_seq") or i
        out.append(d)
    defaults = dict(
        replay_version=1,
        seconds_per_year=365 * 24 * 3600,
        price_staleness_seconds=60,
    )
    defaults.update(kw)
    return replay(out, **defaults)


def rate(eid, ts, rate=RATE_1, m1=1_000_000 * WAD, m2=0, bnum=11, bden=10):
    return ev(eid, ts, "rate_schedule", {
        "rate_per_second": rate,
        "tier1_m1": m1,
        "tier2_m2": m2,
        "liq_bonus_num": bnum,
        "liq_bonus_den": bden,
    })


# ---- 利率切换与时间边界 -----------------------------------------------------


def test_rate_switch_boundary():
    """0..100 秒按 r1 计息，100..200 秒按 r2 计息；切换点属于新区间。"""
    events = [
        rate("r1", 0, RATE_1),
        ev("open", 0, "open_position", {"position_id": "p"}),
        ev("borrow", 0, "borrow", {"position_id": "p", "amount": 100 * WAD}),
        rate("r2", 100, RATE_2),
        ev("tick", 200, "price_update", {"asset": "X", "price": WAD}),
    ]
    rep = run(events)
    debt = rep["positions"][0]["debt"]
    # 第一段：floor(100e18*1e-9*100) = 1e13
    expected_after_first = 100 * WAD + 10_000_000_000_000
    d1 = expected_after_first
    # 第二段：d1 * r2 * 100 / WAD（整段一次 floor，线性非复利）
    expected = d1 + ((d1 * RATE_2 * 100) // WAD)
    assert debt == expected
    # 明确时间边界：t=100 的利率事件适用于 [100,200) 整段（切换点属于新区间）
    assert expected > 100 * WAD
    # 若切换推迟到 t=101，第二秒段少计 1 秒 r2、多计 1 秒 r1
    events_late = [
        rate("r1", 0, RATE_1),
        ev("open", 0, "open_position", {"position_id": "p"}),
        ev("borrow", 0, "borrow", {"position_id": "p", "amount": 100 * WAD}),
        rate("r2b", 101, RATE_2),
        ev("tick", 200, "price_update", {"asset": "X", "price": WAD}),
    ]
    rep_late = run(events_late)
    assert rep_late["positions"][0]["debt"] < expected


def test_zero_delta_same_second_accrues_nothing():
    """同一秒内利率切换 + 借款：dt=0 不计息。"""
    events = [
        rate("r1", 0, RATE_1),
        ev("open", 0, "open_position", {"position_id": "p"}),
        ev("borrow", 0, "borrow", {"position_id": "p", "amount": 50 * WAD}),
        rate("r2", 0, RATE_2),
    ]
    rep = run(events)
    assert rep["positions"][0]["debt"] == 50 * WAD


def test_tiered_rate_marginal_tranches():
    """债务 150：前 100 全额利率，超过部分按 r2 = r1*m1/m2 计息。"""
    m1 = 100 * WAD
    m2 = 400 * WAD  # 第二档为四分之一利率
    events = [
        rate("r1", 0, RATE_1, m1=m1, m2=m2),
        ev("open", 0, "open_position", {"position_id": "p"}),
        ev("borrow", 0, "borrow", {"position_id": "p", "amount": 150 * WAD}),
    ]
    rep = run(events)
    # 没有后续事件 -> max_ts=0，利息为 0；追加一个 t=10 的事件触发计息
    events.append(ev("tick", 10, "price_update", {"asset": "X", "price": WAD}))
    rep = run(events)
    d0 = 150 * WAD
    # 前 100: 100e18*1e-9 = 1e11；超出 50: 50e18 * r1*m1/m2 = 50e18 * 0.25e-9
    per_sec = 100 * WAD * RATE_1 // WAD + 50 * WAD * RATE_1 * m1 // (WAD * m2)
    assert rep["positions"][0]["debt"] == d0 + per_sec * 10


# ---- 健康度边界 ------------------------------------------------------------


def _setup_unhealthy(price: int, debt: int, collateral="0.12", rate_wad=0):
    return [
        rate("r1", 0, rate_wad),
        ev("pETH", 0, "price_update", {"asset": "ETH", "price": 1000 * WAD}),
        ev("open", 0, "open_position", {"position_id": "p"}),
        ev("dep", 0, "deposit", {"position_id": "p", "asset": "ETH",
                                 "amount": parse(collateral)}),
        ev("bor", 0, "borrow", {"position_id": "p", "amount": parse(str(debt))}),
        ev("pdown", 50, "price_update", {"asset": "ETH", "price": price * WAD}),
    ]


def parse(s):
    from app.numerics import parse_amount
    return parse_amount(s)


def test_health_exactly_one_not_liquidatable():
    """抵押价值恰好等于债务（H=1）必须拒绝清算；H<1 才允许。"""
    events = _setup_unhealthy(price=1000, debt=100, collateral="0.1")
    events.append(ev("liq", 50, "liquidate", {"position_id": "p"}))
    rep = run(events)
    assert rep["positions"][0]["health_ratio_wad"] == WAD
    assert rep["liquidations"] == []
    reasons = [s["reason"] for s in rep["skipped"] if s["type"] == "liquidate"]
    assert reasons == ["healthy"]


def test_health_just_below_one_liquidatable():
    events = _setup_unhealthy(price=999, debt=100, collateral="0.1")
    events.append(ev("liq", 50, "liquidate", {"position_id": "p"}))
    rep = run(events)
    assert len(rep["liquidations"]) == 1


# ---- 清算规则 --------------------------------------------------------------


def test_liquidation_pays_at_most_half_with_bonus():
    """单次最多还 50%，奖励 10%，没收抵押价值 = ceil(repay*11/10)。"""
    events = _setup_unhealthy(price=800, debt=100, collateral="0.12")
    events.append(ev("liq", 50, "liquidate", {"position_id": "p", "liquidator": "k"}))
    rep = run(events)
    liq = rep["liquidations"][0]
    assert liq["max_repay"] == 50 * WAD
    assert liq["repaid"] == 50 * WAD
    assert liq["target_seize_value"] == 55 * WAD
    assert liq["seized_value"] == 55 * WAD  # 55/800 = 0.06875 ETH 恰好整除
    assert liq["seized_collateral"]["ETH"] == 68_750_000_000_000_000
    assert liq["debt_after"] == 50 * WAD
    assert liq["shortfall_value"] == 0


def test_repeated_liquidations_each_capped_at_half():
    events = _setup_unhealthy(price=800, debt=100, collateral="0.12")
    events += [
        ev("liq1", 50, "liquidate", {"position_id": "p"}),
        ev("liq2", 51, "liquidate", {"position_id": "p"}),
    ]
    rep = run(events)
    assert [l["event_id"] for l in rep["liquidations"]] == ["liq1", "liq2"]
    l1, l2 = rep["liquidations"]
    assert l1["repaid"] == 50 * WAD
    # 第二次基于剩余债务 50 的 50% = 25
    assert l2["max_repay"] == 25 * WAD
    assert l2["repaid"] == 25 * WAD
    assert rep["positions"][0]["debt"] == 25 * WAD


def test_underwater_position_all_collateral_seized_with_shortfall():
    """抵押品名义价值低于目标：没收全部，按奖励比例反算还贷，记录差额。"""
    events = [
        rate("r1", 0, 0),
        ev("pETH", 0, "price_update", {"asset": "ETH", "price": 1000 * WAD}),
        ev("open", 0, "open_position", {"position_id": "p"}),
        ev("dep", 0, "deposit", {"position_id": "p", "asset": "ETH",
                                 "amount": parse("0.05")}),
        ev("bor", 0, "borrow", {"position_id": "p", "amount": 100 * WAD}),
        ev("pdown", 50, "price_update", {"asset": "ETH", "price": 400 * WAD}),
        ev("liq", 50, "liquidate", {"position_id": "p"}),
    ]
    rep = run(events)
    liq = rep["liquidations"][0]
    # 抵押价值 = 20 < target = ceil(50*1.1)=55（零利率下债务无增长）
    assert liq["collateral_value_before"] == 20 * WAD
    assert liq["seized_value"] == 20 * WAD
    assert liq["repaid"] == 20 * WAD * 10 // 11  # floor
    assert liq["shortfall_value"] == 55 * WAD - 20 * WAD
    assert rep["positions"][0]["collateral"] == {}

def test_stale_price_pauses_liquidation():
    """价格年龄 > 60 秒（严格）暂停清算；恰 60 秒仍可清算。"""
    def case(age, expect_executed: bool):
        events = _setup_unhealthy(price=800, debt=100, collateral="0.12")
        events.append(ev("liq", 50 + age, "liquidate", {"position_id": "p"}))
        rep = run(events)
        assert (len(rep["liquidations"]) == 1) is expect_executed, age
        if not expect_executed:
            assert rep["skipped"][-1]["reason"] == "stale_price"

    case(60, True)
    case(61, False)


def test_missing_price_pauses_liquidation():
    events = [
        rate("r1", 0, RATE_1),
        ev("open", 0, "open_position", {"position_id": "p"}),
        ev("dep", 0, "deposit", {"position_id": "p", "asset": "ETH",
                                 "amount": parse("1")}),
        ev("bor", 0, "borrow", {"position_id": "p", "amount": 100 * WAD}),
        ev("liq", 10, "liquidate", {"position_id": "p"}),
    ]
    rep = run(events)
    assert rep["liquidations"] == []
    assert rep["skipped"][-1]["reason"] == "missing_price"


def test_zero_debt_cannot_be_liquidated():
    events = [
        rate("r1", 0, RATE_1),
        ev("pETH", 0, "price_update", {"asset": "ETH", "price": WAD}),
        ev("open", 0, "open_position", {"position_id": "p"}),
        ev("dep", 0, "deposit", {"position_id": "p", "asset": "ETH",
                                 "amount": parse("1")}),
        ev("liq", 0, "liquidate", {"position_id": "p"}),
    ]
    rep = run(events)
    assert rep["skipped"][-1]["reason"] == "zero_debt"


# ---- 稳定排序 --------------------------------------------------------------


def test_same_timestamp_events_follow_insertion_order():
    """同秒的两个清算必须按入库 seq 稳定排序。"""
    base = [
        rate("r1", 0, RATE_1),
        ev("pETH", 0, "price_update", {"asset": "ETH", "price": 1000 * WAD}),
        ev("open1", 0, "open_position", {"position_id": "a"}),
        ev("dep1", 0, "deposit", {"position_id": "a", "asset": "ETH",
                                  "amount": parse("0.12")}),
        ev("bor1", 0, "borrow", {"position_id": "a", "amount": 100 * WAD}),
        ev("open2", 0, "open_position", {"position_id": "b"}),
        ev("dep2", 0, "deposit", {"position_id": "b", "asset": "ETH",
                                  "amount": parse("0.12")}),
        ev("bor2", 0, "borrow", {"position_id": "b", "amount": 100 * WAD}),
        ev("pdown", 10, "price_update", {"asset": "ETH", "price": 800 * WAD}),
    ]
    # 两个清算同 ts，顺序由入库顺序（seq）决定：b 先入库
    base.append(ev("liq-b", 10, "liquidate", {"position_id": "b"}))
    base.append(ev("liq-a", 10, "liquidate", {"position_id": "a"}))
    rep = run(base)
    order = [(l["event_id"], l["position_id"]) for l in rep["liquidations"]]
    assert order == [("liq-b", "b"), ("liq-a", "a")]

    # 调换入库顺序，结果必须随之改变（证明不是按 id/字典序）
    base[-2], base[-1] = base[-1], base[-2]
    rep2 = run(base)
    order2 = [(l["event_id"], l["position_id"]) for l in rep2["liquidations"]]
    assert order2 == [("liq-a", "a"), ("liq-b", "b")]


def test_late_events_recompute_changes_history():
    """迟到的早期事件按 (ts, seq) 插入正确位置，改变后续清算结果。"""
    base = [
        rate("r1", 0, RATE_1),
        ev("pETH", 0, "price_update", {"asset": "ETH", "price": 1000 * WAD}),
        ev("open", 0, "open_position", {"position_id": "p"}),
        ev("dep", 0, "deposit", {"position_id": "p", "asset": "ETH",
                                 "amount": parse("0.1")}),
        ev("bor", 0, "borrow", {"position_id": "p", "amount": 90 * WAD}),
        ev("pdown", 50, "price_update", {"asset": "ETH", "price": 800 * WAD}),
        ev("liq", 60, "liquidate", {"position_id": "p"}),
    ]
    rep1 = run(list(base))
    assert len(rep1["liquidations"]) == 1

    # 迟到事件：ts=40 的额外存款（seq 更大，但时间更早）
    base.append(ev("dep-late", 40, "deposit",
                   {"position_id": "p", "asset": "ETH", "amount": parse("0.05")}))
    rep2 = run(list(base), replay_version=2)
    # 抵押价值由 80 变为 120 > 债务，清算不再发生
    assert rep2["replay_version"] == 2
    assert rep2["liquidations"] == []
    # processed_event_ids 仍按时间+seq排序，迟到者排在 t=40 位置
    assert rep2["processed_event_ids"].index("dep-late") < rep2["processed_event_ids"].index("liq")


# ---- 其他跳过路径 ----------------------------------------------------------


def test_skip_paths():
    events = [
        rate("bad", 0, RATE_1, bnum=9, bden=10),  # num<den
        ev("dup1", 1, "open_position", {"position_id": "x"}),
        ev("dup2", 2, "open_position", {"position_id": "x"}),
        ev("unk", 3, "deposit", {"position_id": "ghost", "asset": "ETH",
                                 "amount": WAD}),
        ev("badp", 4, "price_update", {"asset": "ETH", "price": 0}),
        ev("wd", 5, "withdraw", {"position_id": "x", "asset": "ETH",
                                 "amount": WAD}),
    ]
    rep = run(events)
    reasons = {s["event_id"]: s["reason"] for s in rep["skipped"]}
    assert reasons == {
        "bad": "invalid_rate_schedule",
        "dup2": "duplicate_position",
        "unk": "unknown_position",
        "badp": "invalid_price",
        "wd": "insufficient_collateral",
    }
