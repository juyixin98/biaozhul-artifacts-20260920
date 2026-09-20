"""Rebuild the projection from immutable history and compare with incremental."""

import uuid

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import ConsentEvent, ConsentState
from app.services_rebuild import rebuild_organization


def _eid():
    return f"evt-{uuid.uuid4()}"


def _seed_history(client, org, purpose, policy_v1):
    h = org["admin"]
    # subject a: grant(v1) -> withdraw -> grant(v1) ; subject b: grant only;
    # subject c: grant with expiry.
    client.post("/api/v1/events/grant",
                json={"event_id": _eid(), "expected_version": 0, "subject_ref": "a",
                      "purpose_key": "marketing", "policy_version": 1}, headers=h)
    client.post("/api/v1/events/withdrawal",
                json={"event_id": _eid(), "expected_version": 1, "subject_ref": "a",
                      "purpose_key": "marketing"}, headers=h)
    client.post("/api/v1/events/grant",
                json={"event_id": _eid(), "expected_version": 2, "subject_ref": "a",
                      "purpose_key": "marketing", "policy_version": 1}, headers=h)
    client.post("/api/v1/events/grant",
                json={"event_id": _eid(), "expected_version": 0, "subject_ref": "b",
                      "purpose_key": "marketing", "policy_version": 1}, headers=h)
    client.post("/api/v1/events/grant",
                json={"event_id": _eid(), "expected_version": 0, "subject_ref": "c",
                      "purpose_key": "marketing", "policy_version": 1}, headers=h)


def _snapshot_states(organization_id: int) -> dict:
    db = SessionLocal()
    try:
        rows = db.execute(
            select(ConsentState).where(
                ConsentState.organization_id == organization_id
            )
        ).scalars()
        out = {}
        for s in rows:
            # Key by the subject pseudonym + purpose so erasure/rebuild is comparable.
            out[(s.subject_id, s.purpose_id)] = {
                "status": s.status,
                "version": s.version,
                "policy_version_id": s.policy_version_id,
                "granted_event_id": s.granted_event_id,
                "granted_event_sequence": s.granted_event_sequence,
                "withdrawn_event_id": s.withdrawn_event_id,
                "withdrawn_event_sequence": s.withdrawn_event_sequence,
            }
        return out
    finally:
        db.close()


def test_rebuild_matches_incremental_projection(client, org, purpose, policy_v1):
    _seed_history(client, org, purpose, policy_v1)
    before = _snapshot_states(org["id"])

    db = SessionLocal()
    try:
        result = rebuild_organization(db, org["id"])
        db.commit()
    finally:
        db.close()

    assert result["replayed_events"] == 5
    after = _snapshot_states(org["id"])
    assert before == after

    # Behaviour is preserved through the HTTP layer too.
    h = org["admin"]
    for ref, valid in (("a", True), ("b", True), ("c", True)):
        assert client.get(
            f"/api/v1/verify?subject_ref={ref}&purpose_key=marketing", headers=h
        ).json()["valid"] is valid


def test_rebuild_endpoint_recovers_from_manually_corrupted_projection(
    client, org, purpose, policy_v1
):
    from sqlalchemy import delete
    _seed_history(client, org, purpose, policy_v1)

    # Simulate projection loss/corruption (bypassing normal code) while history
    # is intact.
    db = SessionLocal()
    try:
        db.execute(delete(ConsentState).where(
            ConsentState.organization_id == org["id"]))
        db.commit()
    finally:
        db.close()
    h = org["admin"]
    assert client.get(
        "/api/v1/verify?subject_ref=b&purpose_key=marketing", headers=h
    ).json()["status"] == "no_record"

    r = client.post("/api/v1/rebuild", headers=h)
    assert r.status_code == 200
    assert r.json()["materialized"] == 3

    body = client.get(
        "/api/v1/verify?subject_ref=a&purpose_key=marketing", headers=h
    ).json()
    assert body["valid"] is True
    assert body["basis"]["grant_event_sequence"] == 3  # latest grant for a


def test_events_remain_after_rebuild(client, org, purpose, policy_v1):
    _seed_history(client, org, purpose, policy_v1)
    db = SessionLocal()
    try:
        n_events = db.execute(
            select(func.count())
            .select_from(ConsentEvent)
        ).scalar_one()
        assert n_events == 5
    finally:
        db.close()
