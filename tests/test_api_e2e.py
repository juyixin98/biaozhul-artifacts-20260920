"""端到端：FastAPI HTTP 接口（只读）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as api
from app.chain import make_swap_id
from app.config import ChainConfig


@pytest.fixture()
def client(chains):
    api.CONFIGS = {
        leg: ChainConfig(
            name=leg,
            rpc_url=chains.clients[leg].rpc_url,
            chain_id=chains.clients[leg].chain_id,
            address=chains.clients[leg].address,
            deploy_block=chains.clients[leg].deploy_block,
        )
        for leg in ("alpha", "beta")
    }
    with TestClient(api.app) as tc:
        yield tc


def test_health(client):
    r = client.get("/health").json()
    assert r["chains"]["alpha"]["reachable"] is True
    assert r["chains"]["beta"]["reachable"] is True
    assert r["chains"]["alpha"]["block_number"] >= 0


def test_config_endpoint(client):
    r = client.get("/config").json()
    assert r["alpha"]["chain_id"] == 31337
    assert r["beta"]["chain_id"] == 31338
    assert r["alpha"]["address"].startswith("0x")


def test_swap_endpoint_full_flow(client, chains):
    sid, _, _ = chains.lock_both("apiflow01", beta_ttl=300, delta=60)
    r = client.get(f"/swap/{sid.hex()}").json()
    assert r["status"] == "LOCKED_BOTH"
    assert r["legs"]["alpha"]["state"] == "Locked"
    assert r["legs"]["beta"]["state"] == "Locked"
    assert r["time_plan"]["deadline_order_ok"] is True

    chains.clients["beta"].claim(chains.keys["beta"]["receiver"], sid, chains.preimage)
    r = client.get("/swap/{0}".format("0x" + sid.hex())).json()
    assert r["status"] == "BETA_CLAIMED_ALPHA_PENDING"
    assert r["legs"]["beta"]["preimage"] == "0x" + chains.preimage.hex()


def test_swaps_listing_contains_locked_swap(client, chains):
    sid = make_swap_id("api-listing-01")
    chains.clients["alpha"].lock(
        chains.keys["alpha"]["sender"], sid,
        chains.receiver_addr("alpha"), chains.hash_lock,
        chains.clients["alpha"].now() + 300, 10**15,
    )
    r = client.get("/swaps").json()
    assert r["count"] >= 1
    assert any(s["swap_id"] == "0x" + sid.hex() for s in r["swaps"])


def test_swap_bad_hex_rejected(client):
    assert client.get("/swap/zzzz").status_code == 400
    assert client.get("/swap/0x12").status_code == 400


def test_swap_endpoint_reports_paused_chain(client, chains):
    sid, _, _ = chains.lock_both("apipause01", beta_ttl=300, delta=60)
    chains.pause("alpha")
    try:
        r = client.get(f"/swap/{sid.hex()}").json()
        assert r["status"] == "CHAIN_UNREACHABLE"
        assert r["legs"]["alpha"]["reachable"] is False
    finally:
        chains.resume("alpha")
