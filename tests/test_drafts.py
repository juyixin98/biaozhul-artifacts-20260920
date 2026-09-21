"""Draft lifecycle: creation, freezing on submit, signing gating."""

from conftest import TEST_ADDRESS, auth, make_custodial_wallet, make_draft, make_user


def test_draft_must_be_submitted_before_signing(client):
    key = make_user(client)
    wallet = make_custodial_wallet(client, key)
    draft = make_draft(client, key, wallet["id"])
    assert draft["status"] == "draft"
    assert draft["content_hash"] is None

    r = client.post(
        "/sign", json={"draft_id": draft["id"]}, headers={**auth(key), "Idempotency-Key": "d-1"}
    )
    assert r.status_code == 409
    assert "submitted" in r.json()["detail"]


def test_submit_freezes_draft_and_sets_content_hash(client):
    key = make_user(client)
    wallet = make_custodial_wallet(client, key)
    draft = make_draft(client, key, wallet["id"])

    r = client.post(f"/drafts/{draft['id']}/submit", headers=auth(key))
    assert r.status_code == 200
    frozen = r.json()
    assert frozen["status"] == "submitted"
    assert frozen["content_hash"]
    assert frozen["submitted_at"]

    # resubmitting is a conflict, not a silent mutation
    r2 = client.post(f"/drafts/{draft['id']}/submit", headers=auth(key))
    assert r2.status_code == 409


def test_draft_validation(client):
    key = make_user(client)
    wallet = make_custodial_wallet(client, key)
    base = {
        "wallet_id": wallet["id"],
        "chain_id": 1,
        "to_address": TEST_ADDRESS,
        "value_wei": "1000",
        "gas_limit": 21000,
        "gas_price_wei": "1000000000",
        "nonce": 0,
    }
    # bad address
    bad = {**base, "to_address": "0x1234"}
    assert client.post("/drafts", json=bad, headers=auth(key)).status_code == 422
    # non-integer wei
    bad = {**base, "value_wei": "1.5"}
    assert client.post("/drafts", json=bad, headers=auth(key)).status_code == 422
    # negative nonce
    bad = {**base, "nonce": -1}
    assert client.post("/drafts", json=bad, headers=auth(key)).status_code == 422
    # zero gas
    bad = {**base, "gas_limit": 0}
    assert client.post("/drafts", json=bad, headers=auth(key)).status_code == 422
