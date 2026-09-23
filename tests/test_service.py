"""服务层测试：十进制解析、验签拒绝、重复 ID、迟到版本化重算、哈希链报告保留。"""
from __future__ import annotations

import pytest

from app import crypto
from app.numerics import WAD
from app.service import ServiceError, normalize_payload


def _rate(event_id="r1", ts=0):
    return event_id, ts, "rate_schedule", {
        "rate_per_second": "0.000000001",
        "tier1_m1": "1000000",
        "tier2_m2": "0",
        "liq_bonus_num": 11,
        "liq_bonus_den": 10,
    }


async def test_ingest_decimal_strings_and_report(service, sign):
    events = [
        sign(*_rate()),
        sign("p1", 0, "price_update", {"asset": "ETH", "price": "1000"}),
        sign("o1", 0, "open_position", {"position_id": "p"}),
        sign("d1", 0, "deposit", {"position_id": "p", "asset": "ETH",
                                  "amount": "0.1"}),
        sign("b1", 0, "borrow", {"position_id": "p", "amount": "100"}),
    ]
    res = await service.ingest_batch(events)
    assert res["accepted"] == 5 and res["replay_version"] == 1

    stored = await service.events()
    assert stored[3]["payload"]["amount"] == WAD // 10

    report = await service.latest_report()
    assert report["body"]["version"] == 1
    assert report["prev_digest"] is None
    assert report["digest"] == crypto.digest_hex(report["body"])
    # 服务器签名可由服务端公钥验证
    service.signing_key.verify_key.verify(
        crypto.report_digest(report["body"]), bytes.fromhex(report["signature"])
    )


async def test_bad_signature_rejected(service, sign, feeder_kp):
    good = sign(*_rate())
    good["sig"] = "00" * 64  # 错误签名
    with pytest.raises(ServiceError) as ei:
        await service.ingest_batch([good])
    assert ei.value.code == "bad_signature"


async def test_unsigned_rejected_when_required(service):
    ev = {"event_id": "x", "ts": 0, "type": "open_position",
          "payload": {"position_id": "p"}}
    with pytest.raises(ServiceError) as ei:
        await service.ingest_batch([ev])
    assert ei.value.code == "bad_signature"


async def test_duplicate_event_id_rejected(service, sign):
    await service.ingest_batch([sign(*_rate())])
    with pytest.raises(ServiceError) as ei:
        await service.ingest_batch([sign(*_rate())])
    assert ei.value.code == "duplicate_event_id" and ei.value.status == 409


async def test_late_event_creates_new_version_old_report_retained(service, sign):
    batch1 = [
        sign(*_rate()),
        sign("p1", 0, "price_update", {"asset": "ETH", "price": "1000"}),
        sign("o1", 0, "open_position", {"position_id": "p"}),
        sign("d1", 0, "deposit", {"position_id": "p", "asset": "ETH",
                                  "amount": "0.1"}),
        sign("b1", 0, "borrow", {"position_id": "p", "amount": "90"}),
        sign("pdown", 50, "price_update", {"asset": "ETH", "price": "800"}),
        sign("liq", 60, "liquidate", {"position_id": "p"}),
    ]
    r1 = await service.ingest_batch(batch1)
    assert r1["late_detected"] is False
    v1 = await service.report(1)
    assert len(v1["body"]["result"]["liquidations"]) == 1

    # 迟到事件（ts=40 < 已处理最大 ts=60）
    late = [sign("d-late", 40, "deposit",
                 {"position_id": "p", "asset": "ETH", "amount": "0.06"})]
    r2 = await service.ingest_batch(late)
    assert r2["late_detected"] is True and r2["replay_version"] == 2

    # 旧报告原样保留，新报告哈希链接在旧摘要之后
    v1_after = await service.report(1)
    assert v1_after["digest"] == v1["digest"]
    v2 = await service.report(2)
    assert v2["prev_digest"] == v1["digest"]
    # 重算后该清算应变为 healthy 被拒
    assert v2["body"]["result"]["liquidations"] == []

    versions = await service.repo.list_report_versions()
    assert [v["version"] for v in versions] == [1, 2]


async def test_in_order_batch_is_not_flagged_late(service, sign):
    await service.ingest_batch([sign(*_rate())])
    res = await service.ingest_batch([sign("p1", 5, "price_update",
                                           {"asset": "ETH", "price": "1"})])
    assert res["late_detected"] is False


def test_normalize_payload_validation():
    with pytest.raises(ValueError):
        normalize_payload("borrow", {"amount": "1.0000000000000000001"})
    p = normalize_payload("rate_schedule", {
        "rate_per_second": "0.5", "tier1_m1": "10", "tier2_m2": "20",
        "liq_bonus_num": 11, "liq_bonus_den": 10})
    assert p["rate_per_second"] == WAD // 2


async def test_bad_amount_returns_400(service, sign):
    ev = sign("bad", 0, "borrow", {"position_id": "p", "amount": "not-a-number"})
    try:
        await service.ingest_batch([ev])
    except ServiceError as e:
        assert e.code == "bad_amount" and e.status == 400
    else:
        raise AssertionError("应当拒绝非法定点")

    ev2 = sign("bad2", 0, "rate_schedule", {
        "rate_per_second": "0.1", "tier1_m1": "1",
        "liq_bonus_num": "abc", "liq_bonus_den": 10})
    try:
        await service.ingest_batch([ev2])
    except ServiceError as e:
        assert e.code == "bad_event"
    else:
        raise AssertionError("应当拒绝非整数奖励参数")
