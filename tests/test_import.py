"""Batch import: <=500, all-or-nothing rollback, repeat identical import replays."""
from __future__ import annotations

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import ConsentEvent
from tests.conftest import auth


def _events(n):
    # Independent grants: unique subject per event so every expected_version=0
    # is genuinely the first state for that (subject, purpose).
    return [
        {
            "event_id": f"e{i}",
            "subject_key": f"user-{i}",
            "purpose": "analytics",
            "action": "grant",
            "expected_version": 0,
        }
        for i in range(n)
    ]


def test_batch_over_500_rejected(client, org):
    client.post("/policies", json={"body": "v"}, headers=auth(org["admin"]))
    r = client.post("/admin/import", json={"events": _events(501)}, headers=auth(org["admin"]))
    assert r.status_code == 422


def test_batch_any_error_rolls_back_whole_batch(client, org):
    client.post("/policies", json={"body": "v"}, headers=auth(org["admin"]))
    events = _events(5)
    # Force a genuine stale-version: after e2 creates user-2's state at
    # version 1, an extra event claiming expected_version=0 for the same key
    # must abort — and roll back the entire batch, including earlier valid rows.
    events[2]["event_id"] = "e2-first"
    events.append(
        {
            "event_id": "e2-late",
            "subject_key": "user-2",
            "purpose": "analytics",
            "action": "grant",
            "expected_version": 0,
        }
    )
    # Batch processor locks in sorted order; the stale check is order-independent.
    r = client.post("/admin/import", json={"events": events}, headers=auth(org["admin"]))
    assert r.status_code == 409, r.text

    db = SessionLocal()
    try:
        count = db.scalar(
            select(func.count())
            .select_from(ConsentEvent)
            .where(ConsentEvent.organization_id == org["org_id"])
        )
        assert count == 0  # nothing survived
    finally:
        db.close()


def test_repeat_identical_import_returns_original_results(client, org):
    client.post("/policies", json={"body": "v"}, headers=auth(org["admin"]))
    payload = {"events": _events(10)}

    first = client.post("/admin/import", json=payload, headers=auth(org["admin"]))
    assert first.status_code == 200, first.text
    assert first.json()["accepted"] == 10
    assert first.json()["replayed"] == 0

    second = client.post("/admin/import", json=payload, headers=auth(org["admin"]))
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["accepted"] == 10
    assert body["replayed"] == 10  # all returned originals, no duplicate rows
    assert all(r["replayed"] for r in body["results"])

    db = SessionLocal()
    try:
        count = db.scalar(
            select(func.count())
            .select_from(ConsentEvent)
            .where(ConsentEvent.organization_id == org["org_id"])
        )
        assert count == 10
    finally:
        db.close()


def test_batch_chained_versions_apply_in_order(client, org):
    client.post("/policies", json={"body": "v"}, headers=auth(org["admin"]))
    events = [
        {"event_id": "a", "subject_key": "u", "purpose": "p", "action": "grant",
         "expected_version": 0},
        {"event_id": "b", "subject_key": "u", "purpose": "p", "action": "withdraw",
         "expected_version": 1},
        {"event_id": "c", "subject_key": "u", "purpose": "p", "action": "grant",
         "expected_version": 2},
    ]
    r = client.post("/admin/import", json={"events": events}, headers=auth(org["admin"]))
    assert r.status_code == 200, r.text

    state = client.get("/subjects/u/consents/p", headers=auth(org["admin"])).json()
    assert state["valid"] is True
    assert state["status"] == "granted"
    assert state["state_version"] == 3
    assert state["basis_event_id"] == "c"
