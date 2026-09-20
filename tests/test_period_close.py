from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from app.database import SessionLocal
from app.errors import ConflictError
from app.schemas import EntryIn, PeriodCloseIn
from app.services.entries import create_entry
from app.services.periods import close_period
from tests.conftest import ACCOUNTANT_KEY, AUDITOR_KEY, LEAD_KEY, auth, balanced_entry


def test_posting_into_closed_period_rejected(client, base_world):
    client.post("/entries", json=balanced_entry("JV-BEFORE"), headers=auth(ACCOUNTANT_KEY))
    r = client.post(
        "/periods/2026-09/close",
        json={"reason": "month end"},
        headers=auth(LEAD_KEY),
    )
    assert r.status_code == 200
    r = client.post("/entries", json=balanced_entry("JV-AFTER"), headers=auth(ACCOUNTANT_KEY))
    assert r.status_code == 409
    assert "closed" in r.json()["error"]["message"]


def test_close_is_idempotent_and_recorded_once(client, base_world):
    h = auth(LEAD_KEY)
    r1 = client.post("/periods/2026-09/close", json={"reason": "end"}, headers=h)
    r2 = client.post("/periods/2026-09/close", json={"reason": "end"}, headers=h)
    assert r1.status_code == r2.status_code == 200
    events = client.get("/periods/2026-09/events", headers=h).json()
    assert [e["action"] for e in events].count("close") == 1


def test_only_lead_can_close_and_reopen(client, base_world):
    r = client.post(
        "/periods/2026-09/close", json={"reason": "x"}, headers=auth(ACCOUNTANT_KEY)
    )
    assert r.status_code == 403
    r = client.post(
        "/periods/2026-09/close", json={"reason": "x"}, headers=auth(AUDITOR_KEY)
    )
    assert r.status_code == 403

    client.post("/periods/2026-09/close", json={"reason": "end"}, headers=auth(LEAD_KEY))
    r = client.post(
        "/periods/2026-09/reopen",
        json={"reason": "missing invoice"},
        headers=auth(ACCOUNTANT_KEY),
    )
    assert r.status_code == 403


def test_reopen_requires_reason_and_leaves_audit_trail(client, base_world):
    client.post("/periods/2026-09/close", json={"reason": "end"}, headers=auth(LEAD_KEY))
    r = client.post(
        "/periods/2026-09/reopen", json={"reason": ""}, headers=auth(LEAD_KEY)
    )
    assert r.status_code == 422
    r = client.post(
        "/periods/2026-09/reopen",
        json={"reason": "late vendor invoice approved by CFO"},
        headers=auth(LEAD_KEY),
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_closed"] is False
    events = client.get("/periods/2026-09/events", headers=auth(LEAD_KEY)).json()
    actions = [(e["action"], e["reason"], e["actor"]) for e in events]
    assert ("close", "end", "lead") in actions
    assert ("reopen", "late vendor invoice approved by CFO", "lead") in actions

    # Posting works again after reopen.
    r = client.post("/entries", json=balanced_entry("JV-REOPENED"), headers=auth(ACCOUNTANT_KEY))
    assert r.status_code == 201


def test_close_posting_race_has_deterministic_order(base_world):
    """Posting and closing race on the same period row: the row lock defines a
    strict order. Either the posting lands first and close still succeeds, or
    close lands first and the posting is rejected - never both committed."""

    def poster():
        db = SessionLocal()
        try:
            create_entry(
                db, EntryIn(**balanced_entry("JV-RACECLOSE")), actor="accountant"
            )
            db.commit()
            return "posted"
        except ConflictError:
            db.rollback()
            return "rejected"
        finally:
            db.close()

    def closer():
        db = SessionLocal()
        try:
            close_period(db, "2026-09", PeriodCloseIn(reason="race"), actor="lead")
            db.commit()
            return "closed"
        except ConflictError:
            db.rollback()
            return "rejected"
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(poster)
        f2 = pool.submit(closer)
        post_result = f1.result()
        close_result = f2.result()

    assert close_result == "closed"
    assert post_result in ("posted", "rejected")

    db = SessionLocal()
    try:
        from app.models import JournalEntry, Period

        period = db.get(Period, "2026-09")
        entry_exists = (
            db.query(JournalEntry).filter_by(voucher_no="JV-RACECLOSE").first()
            is not None
        )
        if post_result == "posted":
            assert entry_exists is True
            # if posting won the lock, the entry landed; close committed after.
            assert period.is_closed is True
        else:
            assert entry_exists is False
            assert period.is_closed is True
    finally:
        db.close()
