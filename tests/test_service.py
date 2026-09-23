"""服务层验收: 真实 Ed25519 验签、去重、迟到事件版本化、旧报告保留。"""
from __future__ import annotations

from app.crypto import public_hex
from app.fixedpoint import WAD
from app.repository import MemoryRepo
from app.service import ReplayService

T = 1_700_000_000


def _rate(make, ts, annual, eid=None, seq=0):
    return make(eid or f"rate-{ts}", ts, seq, "RATE_SCHEDULE",
                {"effective_ts": ts, "annual_rate_wad": annual})


def _open(make, pid="A", collateral=10 * WAD, debt=10_000 * WAD,
          threshold=WAD, ts=T, seq=0, eid=None, key=None, bad=False):
    return make(eid or f"open-{pid}", ts, seq, "OPEN",
                {"position_id": pid, "asset": "ETH", "collateral": collateral,
                 "debt": debt, "liquidation_threshold": threshold},
                key=key, bad_signature=bad)


def _price(make, p, ts, eid=None, seq=0):
    return make(eid or f"px-{ts}", ts, seq, "ORACLE",
                {"asset": "ETH", "price": p})


def _liq(make, eid, ts, repay, seq=0, pid="A"):
    return make(eid, ts, seq, "LIQUIDATE", {"position_id": pid, "repay": repay})


def _fresh(make, operator_key, server_key) -> ReplayService:
    return ReplayService(
        MemoryRepo(),
        {public_hex(operator_key): operator_key.public_key()},
        server_key,
    )


def test_initial_ingest_creates_signed_report(svc, make_event, server_key):
    res = svc.ingest([
        _rate(make_event, T, 0, eid="r0"),
        _open(make_event, eid="open-a"),
        _price(make_event, 2_000 * WAD, T, eid="px"),
    ])
    assert res["accepted"] == 3
    assert res["latest_version"] == 1
    assert res["report_changed"] is True
    verify = svc.verify_report(1)
    assert verify["report_signature_valid"] is True
    assert verify["hash_matches_body"] is True
    assert verify["signer_hex"] == public_hex(server_key)


def test_untrusted_signer_impersonation_rejected(svc, make_event, other_key):
    # other_key 实际签名, 但信封 signer 仍是受信任公钥 -> 验签失败
    ev = _open(make_event, eid="open-x", key=other_key)
    res = svc.ingest([ev])
    assert res["accepted"] == 0
    assert res["rejected"][0]["reason"] == "BAD_SIGNATURE"


def test_tampered_payload_rejected(svc, make_event):
    ev = _price(make_event, 2_000 * WAD, T, eid="px")
    ev["payload"]["price"] = 1  # 签名后篡改
    res = svc.ingest([ev])
    assert res["rejected"][0]["reason"] == "BAD_SIGNATURE"


def test_duplicate_event_id_idempotent(svc, make_event):
    ev = _rate(make_event, T, 0, eid="r0")
    first = svc.ingest([ev])
    assert first["accepted"] == 1

    second = svc.ingest([dict(ev)])
    assert second["accepted"] == 0
    assert second["duplicates"] == ["r0"]
    assert second["report_changed"] is False

    third = svc.ingest([dict(ev), dict(ev)])  # 同批次重复 + 库内重复
    assert third["accepted"] == 0
    assert third["duplicates"].count("r0") == 2
    assert svc.repo.get_latest_version() == 1  # 始终无新版本


def test_late_event_triggers_versioned_recompute_old_report_kept(svc, make_event):
    # 第一批把"当前"推进到 T+100, 仓位健康 (T 时价格 2000)
    svc.ingest([
        _rate(make_event, T, 0, eid="r0"),
        _open(make_event, eid="open-a"),
        _price(make_event, 2_000 * WAD, T, eid="px"),
        _price(make_event, 2_000 * WAD, T + 100, eid="px-current"),
    ])
    v1 = svc.repo.get_report(1)
    assert v1["body"]["as_of"] == T + 100
    assert v1["body"]["positions"][0]["health"]["liquidatable"] is False

    # 迟到事件: 时刻 T+50 (< v1 as_of) 的价格下跌, 回填修正历史
    late = _price(make_event, 900 * WAD, T + 50, eid="px-late")
    res = svc.ingest([late])
    assert res["report_changed"] is True
    assert any("LATE_EVENT:px-late" in r for r in res["changed_reasons"])
    assert res["latest_version"] == 2

    kept = svc.repo.get_report(1)
    assert kept["hash"] == v1["hash"]  # 旧报告原样保留
    assert kept["body"]["positions"][0]["health"]["liquidatable"] is False

    # v2 是全量重算: 最新时刻 T+100 的价格仍为 2000 (px-current 后于迟到事件),
    # 因此当前快照依然健康 —— 但 actions 中记录了 T+50 时的价格更新, 哈希已变。
    v2 = svc.repo.get_report(2)
    assert v1["hash"] != v2["hash"]
    assert any(a["event_id"] == "px-late" for a in v2["body"]["actions"])
    assert svc.verify_report(1)["report_signature_valid"] is True
    assert svc.verify_report(2)["report_signature_valid"] is True

    summaries = svc.repo.list_reports()
    assert [s["version"] for s in summaries] == [2, 1]
    assert summaries[1]["change_reason"] == "INITIAL"
    assert summaries[0]["change_reason"] == "LATE_EVENT_RECOMPUTE"


def test_late_price_makes_current_snapshot_liquidatable(make_event, operator_key,
                                                        server_key):
    """迟到价格若晚于所有已知价格, 它同时成为最新价格, 当前快照转为可清算。"""
    svc = ReplayService(
        MemoryRepo(),
        {public_hex(operator_key): operator_key.public_key()},
        server_key,
    )
    svc.ingest([
        _rate(make_event, T, 0, eid="r0"),
        _open(make_event, eid="open-a"),
        _price(make_event, 2_000 * WAD, T, eid="px"),
        _liq(make_event, "l-forward", T + 200, 5_000 * WAD),
    ])
    # T+200 时价格年龄 200s -> STALE_PRICE, 清算被拒, v1 无清算
    v1 = svc.repo.get_report(1)
    assert v1["body"]["liquidations"] == []

    res = svc.ingest([_price(make_event, 900 * WAD, T + 100, eid="px-late")])
    assert "LATE_EVENT:px-late" in res["changed_reasons"][0]
    v2 = svc.repo.get_report(2)
    # 重算后 T+200 的清算看到 T+100 的 900 (年龄 100s 仍陈旧!) -> 仍被拒
    assert v2["body"]["liquidations"] == []
    # 再加一个 T+150 的迟到价格 (年龄 50s, 新鲜), 清算即可成功
    res2 = svc.ingest([_price(make_event, 900 * WAD, T + 150, eid="px-late2")])
    assert any("LATE_EVENT" in r for r in res2["changed_reasons"])
    v3 = svc.repo.get_report(3)
    assert len(v3["body"]["liquidations"]) == 1
    assert svc.repo.get_report(1)["hash"] == v1["hash"]  # 历史版本不动


def test_recompute_deterministic_regardless_of_submission_order(
    make_event, operator_key, server_key
):
    events = [
        _rate(make_event, T, 0, eid="r0"),
        _open(make_event, eid="open-a"),
        _price(make_event, 900 * WAD, T, eid="px"),
        _liq(make_event, "l1", T + 5, 1_000 * WAD),
    ]

    s_incremental = _fresh(make_event, operator_key, server_key)
    for e in events:  # 逐条正序
        s_incremental.ingest([dict(e)])

    s_reversed = _fresh(make_event, operator_key, server_key)
    s_reversed.ingest(list(reversed(events)))  # 一次性逆序 (含迟到事件)

    h1 = s_incremental.repo.get_report(
        s_incremental.repo.get_latest_version()
    )["hash"]
    h2 = s_reversed.repo.get_report(s_reversed.repo.get_latest_version())["hash"]
    assert h1 == h2
    assert s_reversed.repo.get_latest_version() == 1
