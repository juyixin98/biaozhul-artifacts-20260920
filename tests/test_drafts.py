"""Draft lifecycle: editable while open, immutable after submission."""
from __future__ import annotations

from tests.conftest import (
    auth,
    make_custodial,
    make_draft,
    register,
    submit,
)


def test_draft_starts_open_and_is_editable(client):
    _, key = register(client)
    w = make_custodial(client, key)
    d = make_draft(client, key, w["id"], nonce=1, value=10**15)
    assert d["status"] == "open"

    r = client.patch(
        f"/drafts/{d['id']}", headers=auth(key), json={"value_wei": 2 * 10**15}
    )
    assert r.status_code == 200
    assert int(r.json()["value_wei"]) == 2 * 10**15
    assert r.json()["status"] == "open"


def test_draft_freezes_on_submit_and_is_immutable(client, no_cooldown):
    _, key = register(client)
    w = make_custodial(client, key)
    d = make_draft(client, key, w["id"], nonce=2)
    r = submit(client, key, d["id"], "idem-freeze-000000001")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "success"

    # Frozen draft rejects content changes.
    r = client.patch(
        f"/drafts/{d['id']}", headers=auth(key), json={"value_wei": 1}
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "draft_frozen"

    g = client.get(f"/drafts/{d['id']}", headers=auth(key)).json()
    assert g["status"] == "frozen"
    assert g["frozen_at"] is not None


def test_open_draft_can_be_deleted_but_frozen_cannot(client, no_cooldown):
    _, key = register(client)
    w = make_custodial(client, key)
    d = make_draft(client, key, w["id"], nonce=3)
    r = client.delete(f"/drafts/{d['id']}", headers=auth(key))
    assert r.status_code == 204

    d2 = make_draft(client, key, w["id"], nonce=4)
    submit(client, key, d2["id"], "idem-delete-000000002")
    r = client.delete(f"/drafts/{d2['id']}", headers=auth(key))
    assert r.status_code == 409


def test_draft_cannot_reference_other_users_wallet(client):
    _, k1 = register(client, "alice")
    _, k2 = register(client, "bob")
    w = make_custodial(client, k1)
    r = client.post(
        "/drafts",
        headers=auth(k2),
        json={
            "wallet_id": w["id"],
            "chain_id": 31337,
            "to_address": "0x30BB604CCC63a0c8B0E50f7d9117bDA6AEBAA5A8",
            "value_wei": 1,
            "gas": 21000,
            "gas_price_wei": 1,
            "nonce": 0,
        },
    )
    assert r.status_code == 404


def test_draft_rejects_bad_address_checksum(client):
    _, key = register(client)
    w = make_custodial(client, key)
    r = client.post(
        "/drafts",
        headers=auth(key),
        json={
            "wallet_id": w["id"],
            "chain_id": 1,
            # lowercase address is accepted (treated as non-checksummed);
            # a mixed-case address with wrong checksum must be rejected.
            "to_address": "0x30BB604CCC63a0c8B0E50f7d9117bDA6AEBAA5A8",
            "value_wei": 1,
            "gas": 21000,
            "gas_price_wei": 1,
            "nonce": 0,
        },
    )
    # correct checksum -> 201
    assert r.status_code == 201

    r = client.post(
        "/drafts",
        headers=auth(key),
        json={
            "wallet_id": w["id"],
            "chain_id": 1,
            "to_address": "0x30bB604CCC63a0c8B0E50f7d9117bDA6AEBAA5A8",
            "value_wei": 1,
            "gas": 21000,
            "gas_price_wei": 1,
            "nonce": 1,
        },
    )
    assert r.status_code == 422


def test_negative_value_rejected(client):
    _, key = register(client)
    w = make_custodial(client, key)
    r = client.post(
        "/drafts",
        headers=auth(key),
        json={
            "wallet_id": w["id"],
            "chain_id": 1,
            "to_address": "0x30BB604CCC63a0c8B0E50f7d9117bDA6AEBAA5A8",
            "value_wei": -1,
            "gas": 21000,
            "gas_price_wei": 1,
            "nonce": 0,
        },
    )
    assert r.status_code == 422
