"""FastAPI 端到端测试：建池、报价、快照绑定、验签、只追加存储、错误码。"""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("CLMM_DB_PATH", tmp.name)
    # 在导入前打补丁：db.connect 默认读取 DB_PATH
    from app import db

    monkeypatch.setattr(db, "DB_PATH", tmp.name)
    from app.main import app

    with TestClient(app) as c:
        c.app.state.db.close()
        c.app.state.db = db.connect(tmp.name)
        yield c
    c.app.state.db.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(tmp.name + suffix)
        except FileNotFoundError:
            pass


POOL_BODY = {
    "pool_id": "demo",
    "token0": "USDC",
    "token1": "WETH",
    "fee_ppm": 3000,
    "sqrt_price_x96": "79228162514264337593543950336",
    "positions": [
        {"lower_tick": -100, "upper_tick": 0, "liquidity": "1000000000000000000"},
        {"lower_tick": 0, "upper_tick": 100, "liquidity": "3000000000000000000"},
    ],
}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_create_pool_and_get_snapshot(client):
    r = client.post("/pools", json=POOL_BODY)
    assert r.status_code == 201, r.text
    snap = r.json()
    assert len(snap["snapshot_hash"]) == 64
    r2 = client.get("/pools/demo/snapshot")
    assert r2.json()["snapshot_hash"] == snap["snapshot_hash"]


def test_duplicate_pool_rejected(client):
    assert client.post("/pools", json=POOL_BODY).status_code == 201
    assert client.post("/pools", json=POOL_BODY).status_code == 400


def test_quote_binds_snapshot_and_verifies(client):
    snap = client.post("/pools", json=POOL_BODY).json()
    body = {"pool_id": "demo", "zero_for_one": False, "amount_in": "1000000000000000000"}
    r = client.post("/quotes", json=body)
    assert r.status_code == 201, r.text
    data = r.json()
    payload, sig = data["payload"], data["signature"]
    assert payload["snapshot_hash"] == snap["snapshot_hash"]
    assert payload["result"]["input_reconciled"] is True
    assert len(payload["result"]["segments"]) >= 1

    # HMAC 验签通过
    v = client.post("/quotes/verify", json={"payload": payload, "signature": sig})
    assert v.json()["valid"] is True
    # 篡改输出后验签失败
    bad = {**payload, "result": {**payload["result"], "amount_out_total": "1"}}
    assert client.post("/quotes/verify", json={"payload": bad, "signature": sig}).json()["valid"] is False


def test_quote_persisted_and_replayable(client):
    client.post("/pools", json=POOL_BODY)
    body = {"pool_id": "demo", "zero_for_one": True, "amount_in": "500000000000000000"}
    r1 = client.post("/quotes", json=body).json()
    qid = r1["payload"]["quote_id"]
    r2 = client.get(f"/quotes/{qid}")
    assert r2.status_code == 200
    assert r2.json()["payload"]["result"] == r1["payload"]["result"]


def test_quotes_do_not_mutate_pool(client):
    snap = client.post("/pools", json=POOL_BODY).json()
    for zfo in (True, False):
        client.post("/quotes", json={"pool_id": "demo", "zero_for_one": zfo, "amount_in": "999999999999999999999"})
    again = client.get("/pools/demo/snapshot").json()
    assert again["snapshot_hash"] == snap["snapshot_hash"]
    assert again["sqrt_price_x96"] == snap["sqrt_price_x96"]


def test_quote_unknown_pool_404(client):
    r = client.post("/quotes", json={"pool_id": "nope", "zero_for_one": True, "amount_in": "1"})
    assert r.status_code == 404


def test_quote_bad_inputs_400(client):
    client.post("/pools", json=POOL_BODY)
    # 0 与非数字被请求模型拒绝（422）
    for bad in ("0", "abc"):
        r = client.post("/quotes", json={"pool_id": "demo", "zero_for_one": True, "amount_in": bad})
        assert r.status_code == 422
    # 语法合法但方向非法的 limit_tick 被引擎拒绝（400）
    r = client.post(
        "/quotes",
        json={"pool_id": "demo", "zero_for_one": True, "amount_in": "1", "limit_tick": 5},
    )
    assert r.status_code == 400


def test_fee_100pct_pool_rejected_at_api(client):
    body = {**POOL_BODY, "pool_id": "bad", "fee_ppm": 1_000_000}
    assert client.post("/pools", json=body).status_code == 422


def test_gap_pool_returns_unfilled_input(client):
    body = {
        "pool_id": "gap", "token0": "A", "token1": "B", "fee_ppm": 3000,
        "sqrt_price_x96": "79228162514264337593543950336",
        "positions": [
            {"lower_tick": -100, "upper_tick": -1, "liquidity": "1000000000000"},
            {"lower_tick": 1, "upper_tick": 100, "liquidity": "1000000000000"},
        ],
    }
    client.post("/pools", json=body)
    r = client.post("/quotes", json={"pool_id": "gap", "zero_for_one": False, "amount_in": "999999999999999"}).json()
    res = r["payload"]["result"]
    assert res["stop_reason"] == "incomplete_gap"
    assert res["segments"] == []
    assert res["amount_in_unfilled"] == "999999999999999"
    assert res["fee_total"] == "0"
