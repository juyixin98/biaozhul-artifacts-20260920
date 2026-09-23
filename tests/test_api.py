"""HTTP 集成测试: FastAPI TestClient + 内存仓储 (不依赖 PostgreSQL)。"""
from __future__ import annotations

import os

import pytest

# 必须在导入 app.main 之前设置: 使用内存仓储
os.environ["LIQREPLAY_DATABASE_URL"] = "memory"
os.environ["LIQREPLAY_KEYS_DIR"] = "data/test-keys-api"

from fastapi.testclient import TestClient  # noqa: E402

from app import main as web  # noqa: E402
from app.crypto import generate_private_key, public_hex, sign_payload  # noqa: E402

T = 1_700_000_000
WAD = 10**18


def _signed(key, event_id, ts, seq, action, payload):
    body = {"event_id": event_id, "ts": ts, "seq": seq,
            "action": action, "payload": payload}
    return {**body, "signer": public_hex(key), "signature": sign_payload(key, body)}


@pytest.fixture
def client(operator_key, monkeypatch):
    from app.repository import MemoryRepo
    from app.service import ReplayService

    repo = MemoryRepo()
    server_key = generate_private_key()
    svc = ReplayService(
        repo,
        {public_hex(operator_key): operator_key.public_key()},
        server_key,
    )
    monkeypatch.setattr(web, "_repo", repo)
    monkeypatch.setattr(web, "_service", svc)
    web.app.state.server_public_hex = public_hex(server_key)
    with TestClient(web.app) as c:
        c.key = operator_key
        yield c


def test_full_flow_http(client):
    events = [
        _signed(client.key, "r0", T, 0, "RATE_SCHEDULE",
                {"effective_ts": T, "annual_rate_wad": 0}),
        _signed(client.key, "open-a", T, 1, "OPEN",
                {"position_id": "A", "asset": "ETH",
                 "collateral": 10 * WAD, "debt": 10_000 * WAD,
                 "liquidation_threshold": WAD}),
        _signed(client.key, "px", T, 2, "ORACLE",
                {"asset": "ETH", "price": 900 * WAD}),
        _signed(client.key, "liq", T + 10, 0, "LIQUIDATE",
                {"position_id": "A", "repay": 5_000 * WAD}),
    ]
    r = client.post("/api/v1/events", json=events)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["accepted"] == 4
    assert data["latest_version"] == 1

    r = client.get("/api/v1/reports/latest")
    assert r.status_code == 200
    body = r.json()["body"]
    assert len(body["liquidations"]) == 1

    assert client.get("/api/v1/reports/999").status_code == 404

    r = client.get("/api/v1/liquidations")
    assert r.json()["version"] == 1
    assert r.json()["liquidations"][0]["repay"] == 5_000 * WAD

    r = client.get("/api/v1/reports/1/verify")
    assert r.status_code == 200
    assert r.json()["report_signature_valid"] is True
    assert r.json()["hash_matches_body"] is True

    r = client.get("/api/v1/reports")
    assert [x["version"] for x in r.json()] == [1]

    assert client.get("/health").json()["db"] == "ok"
    assert client.get("/").json()["service"] == "liqreplay"


def test_untrusted_and_bad_signatures_rejected(client):
    # 未知公钥 -> UNTRUSTED_SIGNER
    unknown = _signed(generate_private_key(), "unknown", T, 0, "RATE_SCHEDULE",
                      {"effective_ts": T, "annual_rate_wad": 0})
    # 受信任公钥信封但用别的私钥签名 -> BAD_SIGNATURE
    forged = _signed(generate_private_key(), "forged", T, 1, "OPEN",
                     {"position_id": "X", "asset": "ETH", "collateral": WAD,
                      "debt": WAD, "liquidation_threshold": WAD})
    forged["signer"] = public_hex(client.key)
    r = client.post("/api/v1/events", json=[unknown, forged])
    assert r.status_code == 200
    reasons = {x["event_id"]: x["reason"] for x in r.json()["rejected"]}
    assert reasons == {"unknown": "UNTRUSTED_SIGNER", "forged": "BAD_SIGNATURE"}


def test_empty_batch_400_and_bad_envelope_422(client):
    assert client.post("/api/v1/events", json=[]).status_code == 400
    r = client.post("/api/v1/events", json=[
        {"event_id": "x", "ts": -1, "seq": 0, "action": "OPEN",
         "payload": {}, "signer": "a" * 64, "signature": "b" * 128}
    ])
    assert r.status_code == 422


def test_replay_preview_readonly(client):
    events = [
        _signed(client.key, "r0", T, 0, "RATE_SCHEDULE",
                {"effective_ts": T, "annual_rate_wad": 0}),
        _signed(client.key, "open-a", T, 1, "OPEN",
                {"position_id": "A", "asset": "ETH",
                 "collateral": WAD, "debt": WAD,
                 "liquidation_threshold": WAD}),
        _signed(client.key, "px", T, 2, "ORACLE",
                {"asset": "ETH", "price": WAD}),
    ]
    client.post("/api/v1/events", json=events)
    r = client.get("/api/v1/replay")
    assert r.status_code == 200
    assert r.json()["positions"][0]["position_id"] == "A"
    # 只读预览不产生新版本
    assert client.get("/api/v1/reports").json()[-1]["version"] == 1
