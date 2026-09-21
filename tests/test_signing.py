"""Idempotency, nonce exclusivity, quota and cooldown rules."""
from __future__ import annotations

from tests.conftest import (
    RECIPIENT,
    auth,
    make_custodial,
    make_draft,
    register,
    submit,
)


def test_successful_sign_returns_verified_payload(client, no_cooldown):
    _, key = register(client)
    w = make_custodial(client, key)
    d = make_draft(client, key, w["id"], nonce=10, value=123_456)
    r = submit(client, key, d["id"], "idem-success-000000001")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "success"
    assert body["raw_transaction_hex"].startswith("0x")
    assert body["tx_hash"].startswith("0x")
    assert body["replayed"] is False
    assert int(body["value_wei"]) == 123_456
    assert int(body["nonce"]) == 10


def test_idempotent_retry_returns_same_result(client, no_cooldown):
    _, key = register(client)
    w = make_custodial(client, key)
    d = make_draft(client, key, w["id"], nonce=11)
    first = submit(client, key, d["id"], "idem-retry-0000000001")
    assert first.status_code == 200
    second = submit(client, key, d["id"], "idem-retry-0000000001")
    assert second.status_code == 200
    a, b = first.json(), second.json()
    assert a["id"] == b["id"]
    assert a["tx_hash"] == b["tx_hash"]
    assert a["raw_transaction_hex"] == b["raw_transaction_hex"]
    assert b["replayed"] is True
    # Exactly one sign request exists.
    rows = client.get("/sign-requests", headers=auth(key)).json()
    assert len(rows) == 1


def test_same_idempotency_key_different_content_conflicts(client, no_cooldown):
    _, key = register(client)
    w = make_custodial(client, key)
    d1 = make_draft(client, key, w["id"], nonce=12)
    d2 = make_draft(client, key, w["id"], nonce=13)
    r1 = submit(client, key, d1["id"], "idem-clash-000000001")
    assert r1.status_code == 200
    r2 = submit(client, key, d2["id"], "idem-clash-000000001")
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "idempotency_conflict"
    # The second attempt did not produce a signature nor consume extra quota:
    rows = client.get("/sign-requests", headers=auth(key)).json()
    assert len(rows) == 1


def test_same_nonce_different_content_is_conflict(client, no_cooldown):
    _, key = register(client)
    w = make_custodial(client, key)
    d1 = make_draft(client, key, w["id"], nonce=20, value=10**15)
    d2 = make_draft(client, key, w["id"], nonce=20, value=2 * 10**15)
    r1 = submit(client, key, d1["id"], "idem-nonce-a-0000001")
    assert r1.status_code == 200
    r2 = submit(client, key, d2["id"], "idem-nonce-b-0000002")
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "nonce_conflict"
    # No second signature exists for that nonce.
    rows = [
        x
        for x in client.get("/sign-requests", headers=auth(key)).json()
        if x["nonce"] == 20
    ]
    assert len(rows) == 1
    assert int(rows[0]["value_wei"]) == 10**15


def test_same_nonce_same_content_is_replay_not_new_signature(client, no_cooldown):
    _, key = register(client)
    w = make_custodial(client, key)
    d1 = make_draft(client, key, w["id"], nonce=21, value=555)
    d2 = make_draft(client, key, w["id"], nonce=21, value=555)
    r1 = submit(client, key, d1["id"], "idem-replay-a-000001")
    r2 = submit(client, key, d2["id"], "idem-replay-b-000002")
    assert r1.status_code == r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]
    assert r2.json()["replayed"] is True


def test_same_nonce_on_different_chain_is_allowed(client, no_cooldown):
    _, key = register(client)
    w = make_custodial(client, key)
    d1 = make_draft(client, key, w["id"], nonce=30, chain_id=1)
    d2 = make_draft(client, key, w["id"], nonce=30, chain_id=5)
    r1 = submit(client, key, d1["id"], "idem-chain-a-0000001")
    r2 = submit(client, key, d2["id"], "idem-chain-b-0000002")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["tx_hash"] != r2.json()["tx_hash"]


def test_daily_quota_blocks_when_exceeded(client):
    from app.config import settings

    settings.daily_quota_wei = 1_000
    try:
        _, key = register(client)
        w = make_custodial(client, key)
        d1 = make_draft(client, key, w["id"], nonce=40, value=600)
        r1 = submit(client, key, d1["id"], "idem-quota-1-0000001")
        assert r1.status_code == 200
        d2 = make_draft(client, key, w["id"], nonce=41, value=500)
        r2 = submit(client, key, d2["id"], "idem-quota-2-0000002")
        assert r2.status_code == 429
        assert r2.json()["error"]["code"] == "daily_quota_exceeded"
        # A request that fits within the remaining 400 succeeds.
        d3 = make_draft(client, key, w["id"], nonce=42, value=400)
        r3 = submit(client, key, d3["id"], "idem-quota-3-0000003")
        assert r3.status_code == 200
    finally:
        settings.daily_quota_wei = 10**18


def test_rejected_quota_request_does_not_occupy_anything(client):
    from app.config import settings

    settings.daily_quota_wei = 100
    try:
        _, key = register(client)
        w = make_custodial(client, key)
        d = make_draft(client, key, w["id"], nonce=43, value=200)
        r = submit(client, key, d["id"], "idem-quota-reject-001")
        assert r.status_code == 429
        rows = client.get("/sign-requests", headers=auth(key)).json()
        assert rows == []
    finally:
        settings.daily_quota_wei = 10**18


def test_cooldown_enforced_between_signatures(client):
    from app.config import settings

    settings.cooldown_seconds = 3600
    try:
        _, key = register(client)
        w = make_custodial(client, key)
        d1 = make_draft(client, key, w["id"], nonce=50, value=1)
        r1 = submit(client, key, d1["id"], "idem-cool-1-00000001")
        assert r1.status_code == 200
        d2 = make_draft(client, key, w["id"], nonce=51, value=1)
        r2 = submit(client, key, d2["id"], "idem-cool-2-00000002")
        assert r2.status_code == 429
        assert r2.json()["error"]["code"] == "cooldown_active"
    finally:
        settings.cooldown_seconds = 0


def test_quota_is_per_wallet_not_global(client):
    from app.config import settings

    settings.daily_quota_wei = 1_000
    try:
        _, k1 = register(client, "alice")
        _, k2 = register(client, "bob")
        w1 = make_custodial(client, k1, label="a")
        w2 = make_custodial(client, k2, label="b")
        d1 = make_draft(client, k1, w1["id"], nonce=0, value=800)
        assert submit(client, k1, d1["id"], "idem-pw-a-000000001").status_code == 200
        d2 = make_draft(client, k2, w2["id"], nonce=0, value=800)
        assert submit(client, k2, d2["id"], "idem-pw-b-000000002").status_code == 200
    finally:
        settings.daily_quota_wei = 10**18
