"""HTTP-level tests through the FastAPI app against a live Anvil node."""
from __future__ import annotations

import time

import pytest


def _create_order(client, **overrides):
    body = {
        "makerAmount": 1_000_000_000,
        "takerAmount": 3,
        "nonce": 9001,
        "deadline": int(time.time()) + 3600,
    }
    body.update(overrides)
    r = client.post("/orders", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_health_and_config(client, deployment):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["chainId"] == 31337
    assert data["settlement"].lower() == deployment["settlement"].lower()

    cfg = client.get("/config").json()
    assert set(cfg["accounts"]) >= {"maker", "taker", "feeRecipient"}


def test_create_sign_fill_full_round_trip(client, api):
    chain = api["chain"]
    dep = api["deployment"]
    created = _create_order(client, nonce=9101, makerAmount=1_000, takerAmount=300)

    order = created["order"]
    order["signature"] = created["signature"]

    # status before fill
    r = client.get(f"/orders/{created['orderHash']}")
    assert r.json()["filledMakerAmount"] == 0

    r = client.post("/orders/fill", json={"order": order, "spent": 1_000, "fee": 0})
    assert r.status_code == 200, r.text
    fill = r.json()
    assert fill["takerDue"] == 300
    assert fill["fillStatus"]["filledMakerAmount"] == 1_000

    # balances through the API
    mb = client.get(f"/tokens/{dep['tokenA']}/balance/{order['maker']}").json()["balance"]
    tb = client.get(f"/tokens/{dep['tokenB']}/balance/{order['maker']}").json()["balance"]
    assert mb == 1_000_000 * 10**18 - 1_000
    assert tb == 300


def test_partial_fill_via_api_with_rounding(client):
    # 100 A for 3 B: ceil(33*3/100) = 1 per partial
    created = _create_order(client, nonce=9201, makerAmount=100, takerAmount=3)
    order = created["order"]
    order["signature"] = created["signature"]

    r1 = client.post("/orders/fill", json={"order": order, "spent": 33, "fee": 0}).json()
    r2 = client.post("/orders/fill", json={"order": order, "spent": 33, "fee": 0}).json()
    assert r1["takerDue"] == 1 and r2["takerDue"] == 1
    r3 = client.post("/orders/fill", json={"order": order, "spent": 34, "fee": 0})
    assert r3.status_code == 200, r3.text
    assert r3.json()["takerDue"] == 1  # final dust-clearing fill


def test_replay_rejected_via_api(client):
    created = _create_order(client, nonce=9301, makerAmount=100, takerAmount=30)
    order = created["order"]
    order["signature"] = created["signature"]
    assert client.post("/orders/fill", json={"order": order, "spent": 100}).status_code == 200
    # exact replay: remaining is 0
    r = client.post("/orders/fill", json={"order": order, "spent": 100})
    assert r.status_code == 400
    assert "FillExceedsOrder" in r.json()["detail"]


def test_cancel_via_api_then_fill_rejected(client):
    created = _create_order(client, nonce=9401, makerAmount=100, takerAmount=30)
    order = created["order"]
    order["signature"] = created["signature"]

    r = client.post("/orders/cancel", json={"nonce": 9401})
    assert r.status_code == 200 and r.json()["cancelled"] is True

    r = client.post("/orders/fill", json={"order": order, "spent": 100})
    assert r.status_code == 400
    assert "NonceCancelled" in r.json()["detail"]


def test_expired_order_rejected_via_api(client):
    created = _create_order(
        client, nonce=9501, makerAmount=100, takerAmount=30,
        deadline=int(time.time()) - 1,
    )
    order = created["order"]
    order["signature"] = created["signature"]
    r = client.post("/orders/fill", json={"order": order, "spent": 100})
    assert r.status_code == 400
    assert "OrderExpired" in r.json()["detail"]


def test_tampered_signature_rejected_via_api(client):
    created = _create_order(client, nonce=9601, makerAmount=100, takerAmount=30)
    order = created["order"]
    order["signature"] = created["signature"]
    order["takerAmount"] = 29  # mutate after signing
    r = client.post("/orders/fill", json={"order": order, "spent": 100})
    assert r.status_code == 400
    assert "InvalidSignature" in r.json()["detail"]


def test_fee_cap_enforced_via_api(client):
    dep_fee = None  # feeRecipient account is key[3]
    created = client.post("/orders", json={
        "makerAmount": 1_000, "takerAmount": 250, "nonce": 9701,
        "deadline": int(time.time()) + 3600,
        "maxFeeAmount": 5,
        # fee recipient defaults to zero unless supplied:
        "feeRecipient": client.get("/config").json()["accounts"]["feeRecipient"],
    })
    assert created.status_code == 200, created.text
    order = created.json()["order"]
    order["signature"] = created.json()["signature"]

    r = client.post("/orders/fill", json={"order": order, "spent": 100, "fee": 6})
    assert r.status_code == 400 and "FeeExceedsCap" in r.json()["detail"]
