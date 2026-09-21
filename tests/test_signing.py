"""Signing: offline verification, determinism, idempotency, nonce conflicts."""

from eth_account import Account

from app import signing as signing_mod
from conftest import (
    TEST_ADDRESS,
    TEST_PRIVATE_KEY,
    auth,
    make_custodial_wallet,
    make_draft,
    make_user,
    sign,
    submit,
)


def _signed_draft(client, key, wallet, nonce=0, idem="s-1", **draft_kw):
    draft = make_draft(client, key, wallet["id"], nonce=nonce, **draft_kw)
    submit(client, key, draft["id"])
    return draft, sign(client, key, draft["id"], idem)


def test_sign_and_verify_offline(client):
    key = make_user(client)
    wallet = make_custodial_wallet(client, key, TEST_PRIVATE_KEY)
    _, r = _signed_draft(client, key, wallet)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["signed_tx"].startswith("0x")
    assert body["tx_hash"].startswith("0x")

    # Offline verification: the recovered signer is the wallet address.
    recovered = Account.recover_transaction(body["signed_tx"])
    assert recovered == TEST_ADDRESS


def test_signature_matches_independent_resign(client):
    """The service output must equal an independent eth-account signing."""
    key = make_user(client)
    wallet = make_custodial_wallet(client, key, TEST_PRIVATE_KEY)
    draft = make_draft(
        client, key, wallet["id"], nonce=3, chain_id=5, value_wei="12345", gas_price_wei="7"
    )
    submit(client, key, draft["id"])
    r = sign(client, key, draft["id"], "det-1")
    assert r.status_code == 200

    expected_raw, expected_hash = signing_mod.sign_evm_transaction(
        TEST_PRIVATE_KEY,
        chain_id=5,
        to=TEST_ADDRESS,
        value_wei=12345,
        gas_limit=21000,
        gas_price_wei=7,
        nonce=3,
    )
    assert r.json()["signed_tx"] == expected_raw
    assert r.json()["tx_hash"] == expected_hash


def test_idempotent_retry_returns_same_result(client):
    key = make_user(client)
    wallet = make_custodial_wallet(client, key)
    draft, r1 = _signed_draft(client, key, wallet, idem="idem-1")
    assert r1.status_code == 200

    # Same key + same content → identical stored result (lost-response replay).
    r2 = sign(client, key, draft["id"], "idem-1")
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]
    assert r2.json()["tx_hash"] == r1.json()["tx_hash"]
    assert r2.json()["signed_tx"] == r1.json()["signed_tx"]


def test_idempotency_key_reuse_with_different_content_conflicts(client):
    key = make_user(client)
    wallet = make_custodial_wallet(client, key)
    draft1, r1 = _signed_draft(client, key, wallet, nonce=0, idem="idem-x")
    assert r1.status_code == 200

    draft2 = make_draft(client, key, wallet["id"], nonce=1, value_wei="999")
    submit(client, key, draft2["id"])
    r2 = sign(client, key, draft2["id"], "idem-x")
    assert r2.status_code == 409
    assert "idempotency" in r2.json()["detail"]


def test_nonce_conflict_with_different_transaction(client):
    key = make_user(client)
    wallet = make_custodial_wallet(client, key)
    _, r1 = _signed_draft(client, key, wallet, nonce=7, idem="n-1")
    assert r1.status_code == 200

    # Same wallet + chain + nonce, different recipient → 409.
    other = make_draft(
        client, key, wallet["id"], nonce=7, to="0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
    )
    submit(client, key, other["id"])
    r2 = sign(client, key, other["id"], "n-2")
    assert r2.status_code == 409
    assert "nonce" in r2.json()["detail"]


def test_same_nonce_same_content_replays_result(client):
    key = make_user(client)
    wallet = make_custodial_wallet(client, key)
    draft, r1 = _signed_draft(client, key, wallet, nonce=8, idem="n-3")
    assert r1.status_code == 200
    # Different idempotency key but identical frozen content → same tx, no double sign.
    r2 = sign(client, key, draft["id"], "n-4")
    assert r2.status_code == 200
    assert r2.json()["tx_hash"] == r1.json()["tx_hash"]


def test_sign_response_contains_no_key_material(client):
    key = make_user(client)
    wallet = make_custodial_wallet(client, key, TEST_PRIVATE_KEY)
    _, r = _signed_draft(client, key, wallet, idem="leak-1")
    assert TEST_PRIVATE_KEY not in r.text
    assert TEST_PRIVATE_KEY[2:] not in r.text
    audit = client.get("/audit", headers=auth(key))
    assert audit.status_code == 200
    assert TEST_PRIVATE_KEY[2:] not in audit.text
