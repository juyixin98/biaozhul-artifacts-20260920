"""Crash recovery: interrupted requests resume without double-charging quota
and without ever producing a second, different signed payload."""

from eth_account import Account

import app.services as services
from app.db import SessionLocal
from app.models import DailyUsage, SignRequest
from conftest import TEST_ADDRESS, TEST_PRIVATE_KEY, make_custodial_wallet, make_draft, make_user, sign, submit


def test_crash_after_reservation_resumes_identically(client):
    key = make_user(client)
    wallet = make_custodial_wallet(client, key, TEST_PRIVATE_KEY)
    draft = make_draft(client, key, wallet["id"], nonce=0)
    submit(client, key, draft["id"])

    # Simulate a process crash: pending row + reservation committed, then the
    # process dies before completing the signature.
    original = services._complete_signing

    def crash(db, req):
        raise services.ApiError(500, "simulated process crash")

    services._complete_signing = crash
    try:
        r1 = sign(client, key, draft["id"], "crash-1")
        assert r1.status_code == 500
    finally:
        services._complete_signing = original

    # The request is pending with quota reserved exactly once.
    db = SessionLocal()
    try:
        req = db.query(SignRequest).filter_by(idempotency_key="crash-1").one()
        assert req.status == "pending"
        reserved_once = int(req.quota_reserved_wei)
        usage = db.query(DailyUsage).filter_by(wallet_id=wallet["id"]).one()
        assert int(usage.used_wei) == reserved_once
    finally:
        db.close()

    # Retry with the same idempotency key: resumes and succeeds.
    r2 = sign(client, key, draft["id"], "crash-1")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "succeeded"

    # Quota was NOT charged a second time.
    db = SessionLocal()
    try:
        usage = db.query(DailyUsage).filter_by(wallet_id=wallet["id"]).one()
        assert int(usage.used_wei) == reserved_once
    finally:
        db.close()

    # Deterministic signing: the recovered result is the only possible result.
    recovered = Account.recover_transaction(body["signed_tx"])
    assert recovered == TEST_ADDRESS

    # Replaying again (lost success response) returns the same payload.
    r3 = sign(client, key, draft["id"], "crash-1")
    assert r3.json()["signed_tx"] == body["signed_tx"]
    assert r3.json()["tx_hash"] == body["tx_hash"]

    # And still no extra charge.
    db = SessionLocal()
    try:
        usage = db.query(DailyUsage).filter_by(wallet_id=wallet["id"]).one()
        assert int(usage.used_wei) == reserved_once
    finally:
        db.close()


def test_pending_resume_signature_is_deterministic(client):
    """Two independent completions of the same request yield identical bytes."""
    key = make_user(client)
    wallet = make_custodial_wallet(client, key, TEST_PRIVATE_KEY)
    draft = make_draft(client, key, wallet["id"], nonce=11, chain_id=1, value_wei="42")
    submit(client, key, draft["id"])

    r1 = sign(client, key, draft["id"], "det-crash")
    assert r1.status_code == 200

    # Locally recompute what the signature must be.
    from app.signing import sign_evm_transaction

    expected_raw, expected_hash = sign_evm_transaction(
        TEST_PRIVATE_KEY,
        chain_id=1,
        to=TEST_ADDRESS,
        value_wei=42,
        gas_limit=21000,
        gas_price_wei=10**9,
        nonce=11,
    )
    assert r1.json()["signed_tx"] == expected_raw
    assert r1.json()["tx_hash"] == expected_hash
