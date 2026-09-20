"""Subject erasure: identifiable data and exports removed; ledger anonymised; rebuild stays clean."""
from __future__ import annotations

from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    ConsentEvent,
    ConsentState,
    Subject,
    SubjectExport,
)
from app.schemas import GrantIn
from app import service
from tests.conftest import auth


def _setup_subject(org_id: int, key="ghost"):
    db = SessionLocal()
    try:
        service.publish_policy(db, org_id, "v1")
        service.apply_event(
            db,
            org_id,
            GrantIn(
                event_id="g",
                subject_key=key,
                purpose="analytics",
                action="grant",
                expected_version=0,
            ),
        )
    finally:
        db.close()


def test_queries_after_deletion_return_not_found(client, org):
    _setup_subject(org["org_id"])
    client.post(
        "/subjects/ghost/exports",
        json={"label": "dsar", "payload": {"email": "ghost@example.com"}},
        headers=auth(org["admin"]),
    )

    r = client.post("/subjects/ghost/erase", headers=auth(org["admin"]))
    assert r.status_code == 200, r.text
    assert r.json()["erased"] is True

    # Verification and history both 404 after erasure.
    assert client.get("/subjects/ghost/consents/analytics", headers=auth(org["admin"])).status_code == 404
    assert client.get("/subjects/ghost/events", headers=auth(org["admin"])).status_code == 404
    assert client.get("/subjects/ghost/exports", headers=auth(org["admin"])).status_code == 404

    # Erasing again is also a 404 (no live subject).
    assert client.post("/subjects/ghost/erase", headers=auth(org["admin"])).status_code == 404


def test_personal_data_and_exports_purged_but_ledger_kept(client, org):
    _setup_subject(org["org_id"])
    client.post(
        "/subjects/ghost/exports",
        json={"label": "d", "payload": {"secret": "x"}},
        headers=auth(org["admin"]),
    )
    client.post("/subjects/ghost/erase", headers=auth(org["admin"]))

    db = SessionLocal()
    try:
        # Export copies deleted.
        assert db.scalars(select(SubjectExport)).all() == []
        # Materialised states for the subject deleted.
        assert db.scalars(select(ConsentState)).all() == []
        # Subject anonymised.
        subject = db.scalars(select(Subject)).one()
        assert subject.erased is True and subject.subject_key is None
        # Ledger retained for attribution, but identifying snapshot nulled.
        event = db.scalars(select(ConsentEvent)).one()
        assert event.action == "grant"
        assert event.subject_key_snapshot is None
        assert event.purpose == "analytics"
    finally:
        db.close()


def test_audit_trail_has_no_personal_fields(client, org):
    _setup_subject(org["org_id"])
    client.post("/subjects/ghost/erase", headers=auth(org["admin"]))
    rows = client.get("/audit", headers=auth(org["auditor"])).json()
    actions = {r["action"] for r in rows}
    assert "subject.erase" in actions
    for row in rows:
        # No subject key/email/payload may appear in any audit detail.
        blob = str(row.get("detail") or {})
        assert "ghost" not in blob
        assert "subject_key" not in blob


def test_rebuild_after_deletion_does_not_resurrect(client, org):
    _setup_subject(org["org_id"])
    client.post("/subjects/ghost/erase", headers=auth(org["admin"]))
    r = client.post("/admin/rebuild", headers=auth(org["admin"]))
    assert r.status_code == 200
    assert r.json()["materialized_states"] == 0
    assert r.json()["skipped_tombstoned_subjects"] == 1

    db = SessionLocal()
    try:
        assert db.scalars(select(ConsentState)).all() == []
    finally:
        db.close()
    assert client.get("/subjects/ghost/consents/analytics", headers=auth(org["admin"])).status_code == 404


def test_new_subject_with_same_key_after_erasure_is_fresh(client, org):
    _setup_subject(org["org_id"])
    client.post("/subjects/ghost/erase", headers=auth(org["admin"]))

    client.post("/policies", json={"body": "still here"}, headers=auth(org["admin"]))
    fresh = client.post(
        "/consents",
        json={
            "event_id": "new-g",
            "subject_key": "ghost",
            "purpose": "analytics",
            "action": "grant",
            "expected_version": 0,
        },
        headers=auth(org["admin"]),
    )
    assert fresh.status_code == 201, fresh.text
    state = client.get("/subjects/ghost/consents/analytics", headers=auth(org["admin"])).json()
    assert state["valid"] is True
