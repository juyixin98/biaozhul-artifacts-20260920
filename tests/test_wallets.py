"""Wallet lifecycle: custodial vs watch-only, key secrecy, isolation."""
from __future__ import annotations

from tests.conftest import (
    RECIPIENT,
    TEST_ADDR,
    WATCH_ADDR,
    auth,
    make_custodial,
    make_draft,
    make_watch_only,
    register,
    submit,
)


def test_custodial_wallet_derives_address_and_hides_key(client):
    _, key = register(client, "alice")
    w = make_custodial(client, key)
    assert w["address"] == TEST_ADDR
    assert w["kind"] == "custodial"
    assert w["has_private_key"] is True
    # The private key must not appear anywhere in the response.
    blob = str(w)
    assert "079e6e68" not in blob
    assert "key_ciphertext" not in blob


def test_watch_only_wallet_has_no_key(client):
    _, key = register(client)
    w = make_watch_only(client, key)
    assert w["address"] == WATCH_ADDR
    assert w["has_private_key"] is False
    assert w["kind"] == "watch_only"


def test_custodial_requires_private_key(client):
    _, key = register(client)
    r = client.post(
        "/wallets",
        headers=auth(key),
        json={"label": "x", "kind": "custodial"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "private_key_required"


def test_invalid_private_key_rejected(client):
    _, key = register(client)
    r = client.post(
        "/wallets",
        headers=auth(key),
        json={
            "label": "bad",
            "kind": "custodial",
            "private_key_hex": "0x" + "00" * 32,  # zero key is out of range
        },
    )
    assert r.status_code == 409
    body = r.text
    # No key material echoed back in the error.
    assert "00000000" not in body
    assert r.json()["error"]["code"] == "invalid_private_key"


def test_watch_only_wallet_cannot_sign(client, no_cooldown):
    _, key = register(client)
    w = make_watch_only(client, key)
    d = make_draft(client, key, w["id"])
    r = submit(client, key, d["id"], "idem-watch-only-0001")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "watch_only_cannot_sign"


def test_wallet_listing_isolated_per_user(client):
    _, k1 = register(client, "alice")
    _, k2 = register(client, "bob")
    make_custodial(client, k1, label="a1")
    make_watch_only(client, k2, label="b1")
    l1 = client.get("/wallets", headers=auth(k1)).json()
    l2 = client.get("/wallets", headers=auth(k2)).json()
    assert len(l1) == 1 and l1[0]["label"] == "a1"
    assert len(l2) == 1 and l2[0]["label"] == "b1"


def test_user_cannot_open_other_users_wallet(client):
    _, k1 = register(client, "alice")
    _, k2 = register(client, "bob")
    w = make_custodial(client, k1)
    # Bob sees 404 (existence not disclosed) rather than 403.
    r = client.get(f"/wallets/{w['id']}", headers=auth(k2))
    assert r.status_code == 404


def test_auth_required(client):
    r = client.get("/wallets")
    assert r.status_code == 401
    r = client.post(
        "/wallets",
        headers={"Authorization": "Bearer not-a-real-key"},
        json={"label": "x", "kind": "watch_only", "address": RECIPIENT},
    )
    assert r.status_code == 401
