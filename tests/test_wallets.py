"""Wallet lifecycle: custodial vs watch-only, key hygiene, user isolation."""

from conftest import (
    TEST_ADDRESS,
    TEST_PRIVATE_KEY,
    auth,
    make_custodial_wallet,
    make_draft,
    make_user,
    make_watch_wallet,
)


def test_create_custodial_wallet_never_exposes_key(client):
    key = make_user(client)
    wallet = make_custodial_wallet(client, key, TEST_PRIVATE_KEY)
    assert wallet["address"] == TEST_ADDRESS
    assert wallet["kind"] == "custodial"
    # The private key (raw, 0x-stripped, or ciphertext) appears nowhere.
    body = client.get("/wallets", headers=auth(key)).text
    assert TEST_PRIVATE_KEY not in body
    assert TEST_PRIVATE_KEY[2:] not in body
    assert "encrypted_key" not in body
    assert "private_key" not in body


def test_generate_custodial_wallet(client):
    key = make_user(client)
    wallet = make_custodial_wallet(client, key, None)
    assert wallet["address"].startswith("0x")
    assert len(wallet["address"]) == 42


def test_watch_only_wallet_cannot_sign(client):
    key = make_user(client)
    wallet = make_watch_wallet(client, key)
    draft = make_draft(client, key, wallet["id"])
    client.post(f"/drafts/{draft['id']}/submit", headers=auth(key))
    r = client.post(
        "/sign", json={"draft_id": draft["id"]}, headers={**auth(key), "Idempotency-Key": "wo-1"}
    )
    assert r.status_code == 403
    assert "watch-only" in r.json()["detail"]


def test_cross_user_wallet_access_denied(client):
    key_a = make_user(client)
    key_b = make_user(client)
    wallet_a = make_custodial_wallet(client, key_a)

    # B cannot see A's wallets
    assert client.get("/wallets", headers=auth(key_b)).json() == []
    # B cannot create a draft on A's wallet
    r = client.post(
        "/drafts",
        json={
            "wallet_id": wallet_a["id"],
            "chain_id": 1,
            "to_address": TEST_ADDRESS,
            "value_wei": "1",
            "gas_limit": 21000,
            "gas_price_wei": "1",
            "nonce": 0,
        },
        headers=auth(key_b),
    )
    assert r.status_code == 404


def test_cross_user_draft_and_sign_access_denied(client):
    key_a = make_user(client)
    key_b = make_user(client)
    wallet_a = make_custodial_wallet(client, key_a)
    draft = make_draft(client, key_a, wallet_a["id"])
    client.post(f"/drafts/{draft['id']}/submit", headers=auth(key_a))

    assert client.get(f"/drafts/{draft['id']}", headers=auth(key_b)).status_code == 404
    r = client.post(
        "/sign", json={"draft_id": draft["id"]}, headers={**auth(key_b), "Idempotency-Key": "x-1"}
    )
    assert r.status_code == 404


def test_cross_user_sign_request_access_denied(client):
    key_a = make_user(client)
    key_b = make_user(client)
    wallet_a = make_custodial_wallet(client, key_a)
    draft = make_draft(client, key_a, wallet_a["id"])
    client.post(f"/drafts/{draft['id']}/submit", headers=auth(key_a))
    r = client.post(
        "/sign", json={"draft_id": draft["id"]}, headers={**auth(key_a), "Idempotency-Key": "a-1"}
    )
    req_id = r.json()["id"]
    assert client.get(f"/sign/{req_id}", headers=auth(key_b)).status_code == 404
    assert client.get("/sign", headers=auth(key_b)).json() == []


def test_invalid_api_key_rejected(client):
    assert client.get("/wallets", headers={"X-API-Key": "nope"}).status_code == 401
    assert client.get("/wallets").status_code == 401
