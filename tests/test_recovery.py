"""Crash recovery: pending holds, idempotent retries, quota release/restore."""
from __future__ import annotations

import pytest

from app import services
from app.db import SessionLocal
from app.models import REQ_PENDING, SignRequest
from tests.conftest import (
    auth,
    make_custodial,
    make_draft,
    register,
    submit,
)


def _crash_during_phase2(monkeypatch, user_id: str, draft_id: str, idem: str):
    """Drive submit_sign directly on a worker session, with local signing
    dying after the phase-1 occupation commit. The session is closed (like a
    process whose socket drops), leaving a durable pending row."""
    crash_db = SessionLocal()
    monkeypatch.setattr(
        services,
        "sign_legacy_tx",
        lambda pk, tx: (_ for _ in ()).throw(SystemExit("simulated SIGKILL")),
    )
    with pytest.raises(SystemExit):
        services.submit_sign(
            crash_db,
            user_id=user_id,
            draft_id=draft_id,
            idempotency_key=idem,
            request_id="crash-worker",
        )
    crash_db.close()
    monkeypatch.undo()


def test_crash_after_pending_commit_leaves_recoverable_hold(client, monkeypatch):
    """The occupation committed in phase 1 survives a phase-2 crash. Recovery
    via resume finishes the same row once; quota was never double-debited."""
    from app.config import settings

    settings.daily_quota_wei = 1_000
    settings.cooldown_seconds = 0
    user_id, key = register(client)
    w = make_custodial(client, key)
    d = make_draft(client, key, w["id"], nonce=60, value=400)

    _crash_during_phase2(monkeypatch, user_id, d["id"], "idem-crash-00000001")

    # The occupation survived the crash.
    db = SessionLocal()
    try:
        stuck = db.query(SignRequest).filter_by(draft_id=d["id"]).one()
        assert stuck.status == REQ_PENDING
        assert stuck.quota_day is not None
        assert int(stuck.value_wei) == 400
        stuck_id = stuck.id
    finally:
        db.close()

    # The remaining 600 wei is usable; 601 is not.
    d2 = make_draft(client, key, w["id"], nonce=61, value=600)
    assert submit(client, key, d2["id"], "idem-crash-other-001").status_code == 200
    d2b = make_draft(client, key, w["id"], nonce=62, value=601)
    assert submit(client, key, d2b["id"], "idem-crash-other-002").status_code == 429

    # Resume the stuck row - same id, same hold, signature produced.
    r = client.post(f"/sign-requests/{stuck_id}/resume", headers=auth(key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "success"
    assert body["id"] == stuck_id
    assert body["tx_hash"].startswith("0x")

    # Re-resume is rejected: already finished.
    r = client.post(f"/sign-requests/{stuck_id}/resume", headers=auth(key))
    assert r.status_code == 409


def test_release_pending_returns_quota_and_nonce(client, monkeypatch):
    from app.config import settings

    settings.daily_quota_wei = 1_000
    settings.cooldown_seconds = 0
    user_id, key = register(client)
    w = make_custodial(client, key)
    d = make_draft(client, key, w["id"], nonce=70, value=900)

    _crash_during_phase2(monkeypatch, user_id, d["id"], "idem-release-0000001")

    rows = client.get("/sign-requests", headers=auth(key)).json()
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    stuck_id = rows[0]["id"]

    # While held, quota is exhausted.
    d2 = make_draft(client, key, w["id"], nonce=71, value=200)
    assert submit(client, key, d2["id"], "idem-release-blocked").status_code == 429

    # Release recovers the hold.
    r = client.post(f"/sign-requests/{stuck_id}/release", headers=auth(key))
    assert r.status_code == 200
    assert r.json()["status"] == "released"
    assert r.json()["raw_transaction_hex"] is None

    # Nonce 70 is reusable now, and quota is back to full.
    d3 = make_draft(client, key, w["id"], nonce=70, value=900)
    r = submit(client, key, d3["id"], "idem-release-retry-01")
    assert r.status_code == 200
    assert r.json()["status"] == "success"


def test_lost_response_retry_does_not_double_charge_or_duplicate(client, no_cooldown):
    """Phase 2 commits success, but the HTTP response is lost. Retrying with
    the same idempotency key returns the SAME result, once, a single debit."""
    _, key = register(client)
    w = make_custodial(client, key)
    d = make_draft(client, key, w["id"], nonce=80, value=33)
    first = submit(client, key, d["id"], "idem-lost-000000001").json()
    second = submit(client, key, d["id"], "idem-lost-000000001").json()
    third = submit(client, key, d["id"], "idem-lost-000000001").json()
    assert first["id"] == second["id"] == third["id"]
    assert first["tx_hash"] == second["tx_hash"] == third["tx_hash"]
    rows = client.get("/sign-requests", headers=auth(key)).json()
    assert len(rows) == 1
    assert sum(int(r["value_wei"]) for r in rows) == 33


def test_crash_before_occupation_commit_never_debits(client, monkeypatch):
    """Failure during PHASE 1 (before the pending commit) leaves no row and
    consumes no quota."""
    _, key = register(client)
    w = make_custodial(client, key)
    d = make_draft(client, key, w["id"], nonce=90, value=33)

    def boom(*a, **k):  # noqa: ANN001
        raise RuntimeError("phase1 crash")

    monkeypatch.setattr(services, "_lock_wallet", boom)
    with pytest.raises(RuntimeError):
        submit(client, key, d["id"], "idem-phase1-00000001")
    monkeypatch.undo()

    rows = client.get("/sign-requests", headers=auth(key)).json()
    assert rows == []
    # Retry works and occupies exactly once.
    r = submit(client, key, d["id"], "idem-phase1-00000001")
    assert r.status_code == 200
    assert r.json()["status"] == "success"
