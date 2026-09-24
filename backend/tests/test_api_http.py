"""HTTP 接口测试：FastAPI TestClient 直连已部署在本机 Anvil 的合约。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app import chain as chainlib
from backend.app.config import ANVIL_TEST_PRIVATE_KEYS
from backend.app.main import app

WAD = 10**18
client = TestClient(app)

PK0 = ANVIL_TEST_PRIVATE_KEYS[0]
PK1 = ANVIL_TEST_PRIVATE_KEYS[1]


@pytest.fixture(autouse=True)
def _ensure_deploy(bundle):
    """复用 conftest 的每用例全新部署；返回 bundle 供需要时使用。"""
    return bundle


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert int(body["chain_id"]) == 31337


def test_vault_info_initial_empty():
    r = client.get("/vault/info")
    assert r.status_code == 200
    body = r.json()
    assert body["total_assets"] == 0
    assert body["total_shares"] == 0
    assert body["exchange_rate_x1e18"] == 0
    assert body["dead_shares"] == 1000


def test_full_flow_via_http():
    # 1) 账户0 领水 5000 枚
    r = client.post("/token/mint", json={"address": _addr(PK0), "amount": 5000 * WAD, "private_key": PK0})
    assert r.status_code == 200, r.text
    # 2) 授权
    r = client.post("/token/approve", json={"private_key": PK0, "amount": 2**256 - 1})
    assert r.status_code == 200, r.text
    # 3) 预览首存
    r = client.get(f"/vault/preview/deposit?assets={5000 * WAD}")
    body = r.json()
    assert body["estimated_output"] == 5000 * WAD - 1000
    # 4) 首存（默认 0.5% 滑点）
    r = client.post(
        "/vault/deposit",
        json={"private_key": PK0, "assets": 5000 * WAD},
    )
    assert r.status_code == 200, r.text
    assert r.json()["shares"] == 5000 * WAD - 1000
    # 5) 金库信息：汇率接近 1e18（首存 1:1）
    info = client.get("/vault/info").json()
    assert info["total_assets"] == 5000 * WAD
    assert info["total_shares"] == 5000 * WAD
    assert info["exchange_rate_x1e18"] == 10**18

    # 6) 账户0 全额赎回
    shares = info["total_shares"] - 1000
    r = client.post(
        "/vault/redeem",
        json={"private_key": PK0, "shares": shares},
    )
    assert r.status_code == 200, r.text
    # 死份额损失恰好 1000。
    assert r.json()["assets"] == 5000 * WAD - 1000


def test_slippage_protection_reverts_http():
    # 首存
    client.post("/token/mint", json={"address": _addr(PK0), "amount": 5000 * WAD, "private_key": PK0})
    client.post("/token/approve", json={"private_key": PK0, "amount": 2**256 - 1})
    client.post("/vault/deposit", json={"private_key": PK0, "assets": 5000 * WAD})

    # 账户1 存钱并授权
    client.post("/token/mint", json={"address": _addr(PK1), "amount": 2000 * WAD, "private_key": PK1})
    client.post("/token/approve", json={"private_key": PK1, "amount": 2**256 - 1})
    estimated = client.get(f"/vault/preview/deposit?assets={2000 * WAD}").json()["estimated_output"]

    # 把 min_shares 抬到预估 +1，必然触发 SlippageExceeded。
    r = client.post(
        "/vault/deposit",
        json={"private_key": PK1, "assets": 2000 * WAD, "min_shares": estimated + 1},
    )
    assert r.status_code == 400
    assert "SlippageExceeded" in r.text or "交易回滚" in r.text


def test_account_view_balances():
    client.post("/token/mint", json={"address": _addr(PK0), "amount": 12345, "private_key": PK0})
    r = client.get(f"/account/{_addr(PK0)}")
    assert r.status_code == 200
    body = r.json()
    assert body["token_balance"] >= 12345
    assert body["share_balance"] == 0


def test_invalid_address_returns_400():
    r = client.get("/account/not-an-address")
    assert r.status_code == 400


def _addr(pk: str) -> str:
    from web3 import Web3

    return Web3(Web3.HTTPProvider("http://127.0.0.1:8545")).eth.account.from_key(pk).address
