"""HTTP 层端到端测试（ASGI in-process + 内存仓储，真实密钥/签名）。"""
from __future__ import annotations


from app import crypto


def rate_event():
    return {
        "event_id": "r1", "ts": 0, "type": "rate_schedule",
        "payload": {
            "rate_per_second": "0.000000001", "tier1_m1": "1000000",
            "tier2_m2": "0", "liq_bonus_num": 11, "liq_bonus_den": 10,
        },
    }


def sign(sk, ev):
    ev = dict(ev)
    ev["sig"] = sk.sign(crypto.event_signing_bytes(ev)).signature.hex()
    return ev


async def test_health_and_full_flow(client_factory, feeder_kp, mem_repo):
    async with client_factory() as client:
        h = await client.get("/health")
        assert h.status_code == 200 and h.json()["storage"] == "memory"

        batch = [sign(feeder_kp.signing_key, rate_event())]
        r = await client.post("/events", json={"events": batch})
        assert r.status_code == 200, r.text
        assert r.json()["replay_version"] == 1

        # 坏签名 -> 400
        bad = rate_event()
        bad["event_id"] = "r2"
        bad["sig"] = "ab" * 64
        r = await client.post("/events", json={"events": [bad]})
        assert r.status_code == 400 and r.json()["error"] == "bad_signature"

        # 重复 ID -> 409
        dup = sign(feeder_kp.signing_key, rate_event())
        r = await client.post("/events", json={"events": [dup]})
        assert r.status_code == 409

        # 报告查询
        r = await client.get("/reports/latest")
        assert r.status_code == 200
        assert r.json()["body"]["version"] == 1
        r = await client.get("/reports/99")
        assert r.status_code == 404
        r = await client.get("/reports")
        assert [x["version"] for x in r.json()["reports"]] == [1]

        r = await client.get("/events")
        assert len(r.json()["events"]) == 1

        # 重置
        r = await client.post("/admin/reset")
        assert r.status_code == 200
        r = await client.get("/reports/latest")
        assert r.status_code == 404


async def test_reset_disabled_by_default(client_factory):
    from tests.conftest import make_settings

    async with client_factory(make_settings(allow_reset=False)) as client:
        r = await client.post("/admin/reset")
        assert r.status_code == 404


async def test_signatures_can_be_disabled(client_factory):
    from tests.conftest import make_settings

    async with client_factory(make_settings(require_signatures=False)) as client:
        ev = {"event_id": "o1", "ts": 0, "type": "open_position",
              "payload": {"position_id": "p"}}
        r = await client.post("/events", json={"events": [ev]})
        assert r.status_code == 200
