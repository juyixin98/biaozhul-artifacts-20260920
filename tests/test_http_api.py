"""HTTP 层端到端：FastAPI → 本地 Anvil，覆盖部署、查询、单笔/批量领取与重放。"""

from __future__ import annotations


def _deploy(client):
    resp = client.post("/deploy", json={})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_health_before_deploy(fastapi_client):
    data = fastapi_client.get("/health").json()
    assert data["rpc_connected"] is True
    assert data["chain_id"] == 31337
    assert data["deployed"] is False


def test_deploy_is_idempotent_conflict(fastapi_client):
    info = _deploy(fastapi_client)
    assert info["contract_address"].startswith("0x")
    assert info["merkle_root"].startswith("0x")
    again = fastapi_client.post("/deploy", json={})
    assert again.status_code == 409


def test_allocations_list_and_proof(fastapi_client):
    _deploy(fastapi_client)
    allocs = fastapi_client.get("/allocations").json()
    assert len(allocs) == 7

    p = fastapi_client.get("/allocations/3/proof").json()
    assert p["index"] == 3
    assert isinstance(p["proof"], list) and len(p["proof"]) >= 1
    assert p["claimed"] is False
    assert p["root"].startswith("0x")

    assert fastapi_client.get("/allocations/999/proof").status_code == 404


def test_single_claim_via_http_then_duplicate_rejected(fastapi_client):
    _deploy(fastapi_client)

    ok = fastapi_client.post("/claim", json={"index": 0})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["status"] == 1
    assert body["claimed"][0][0] == 0

    claimed = fastapi_client.get("/allocations/0/proof").json()
    assert claimed["claimed"] is True

    dup = fastapi_client.post("/claim", json={"index": 0})
    assert dup.status_code == 422


def test_batch_happy_path(fastapi_client):
    _deploy(fastapi_client)
    resp = fastapi_client.post("/claim/batch", json={"indexes": [1, 2, 3]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["claimed"]) == 3
    for idx in (1, 2, 3):
        assert fastapi_client.get(f"/allocations/{idx}/proof").json()["claimed"] is True


def test_batch_with_duplicate_index_atomically_rejected(fastapi_client):
    _deploy(fastapi_client)
    # 批次内重复 index：estimateGas 阶段即被节点拒绝，交易根本没有发出
    resp = fastapi_client.post("/claim/batch", json={"indexes": [4, 4]})
    assert resp.status_code == 422
    assert resp.json()["tx_sent"] is False

    # 没有任何部分状态
    assert fastapi_client.get("/allocations/4/proof").json()["claimed"] is False


def test_batch_with_one_already_claimed_atomically_rejected(fastapi_client):
    _deploy(fastapi_client)
    assert fastapi_client.post("/claim", json={"index": 5}).status_code == 200

    resp = fastapi_client.post("/claim/batch", json={"indexes": [5, 6]})
    assert resp.status_code == 422
    # 5 已领（单笔），6 不能被失败的批次带上
    assert fastapi_client.get("/allocations/5/proof").json()["claimed"] is True
    assert fastapi_client.get("/allocations/6/proof").json()["claimed"] is False


def test_claim_before_deploy_returns_409(fastapi_client):
    assert fastapi_client.post("/claim", json={"index": 0}).status_code == 409
    assert fastapi_client.post("/claim/batch", json={"indexes": [0]}).status_code == 409
