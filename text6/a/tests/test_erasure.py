"""Subject erasure: identifiable mapping + export copies removed, history kept
in pseudonymous form, audit logs never carry personal fields."""

import uuid


def _eid():
    return f"evt-{uuid.uuid4()}"


def test_erasure_clears_identifiable_data_and_retains_pseudonymous_history(
    client, org, purpose, policy_v1
):
    from app.db import SessionLocal
    from sqlalchemy import func, select

    from app.models import (
        AuditLog, ConsentEvent, ConsentState, Subject, SubjectExportCopy,
    )

    h = org["admin"]
    victim = "victim-user"
    client.post("/api/v1/events/grant",
                json={"event_id": _eid(), "expected_version": 0, "subject_ref": victim,
                      "purpose_key": "marketing", "policy_version": 1}, headers=h)
    client.post("/api/v1/subjects/victim-user/export-copies",
                json={"copy_label": "warehouse"}, headers=h)

    r = client.delete("/api/v1/subjects/victim-user", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["deleted_subjects"] == 1
    assert body["deleted_states"] == 1
    assert body["retained_history_events"] == 1

    # Verify now behaves as if no identifiable record exists.
    r = client.get("/api/v1/verify?subject_ref=victim-user&purpose_key=marketing",
                   headers=h)
    assert r.json()["status"] == "no_record"

    # Identifiable history lookup is gone...
    r = client.get("/api/v1/history?subject_ref=victim-user&purpose_key=marketing",
                   headers=h)
    assert r.status_code == 404

    db = SessionLocal()
    try:
        # ...subjects/states/export copies physically removed...
        assert db.execute(select(func.count()).select_from(Subject)).scalar_one() == 0
        assert db.execute(select(func.count()).select_from(ConsentState)).scalar_one() == 0
        assert db.execute(
            select(func.count()).select_from(SubjectExportCopy)).scalar_one() == 0
        # ...immutable events retained...
        assert db.execute(
            select(func.count()).select_from(ConsentEvent)).scalar_one() == 1
        # ...and no event/audit row contains the identifiable reference.
        events = db.execute(select(ConsentEvent)).scalars().all()
        assert all(victim not in (e.subject_pseudonym or "") for e in events)
        audit = db.execute(select(AuditLog)).scalars().all()
        assert all(victim not in (a.detail_json or "") for a in audit)
        assert all(victim not in (a.actor_pseudonym or "") for a in audit)
    finally:
        db.close()


def test_delayed_retry_after_erasure_returns_gone_and_does_not_recreate(
    client, org, purpose, policy_v1
):
    h = org["admin"]
    payload = {"event_id": _eid(), "expected_version": 0, "subject_ref": "ghosted",
               "purpose_key": "marketing", "policy_version": 1}
    client.post("/api/v1/events/grant", json=payload, headers=h)
    client.delete("/api/v1/subjects/ghosted", headers=h)

    # Late retry of the original event cannot recreate the subject.
    r = client.post("/api/v1/events/grant", json=payload, headers=h)
    assert r.status_code == 410
    assert r.json()["error"] == "subject_erased"

    r = client.get("/api/v1/verify?subject_ref=ghosted&purpose_key=marketing",
                   headers=h)
    assert r.json()["status"] == "no_record"


def test_pseudonymous_history_remains_auditable_after_erasure(
    client, org, purpose, policy_v1
):
    h = org["admin"]
    a = org["auditor"]
    victim = "auditable-user"
    client.post("/api/v1/events/grant",
                json={"event_id": _eid(), "expected_version": 0, "subject_ref": victim,
                      "purpose_key": "marketing", "policy_version": 1}, headers=h)
    client.delete("/api/v1/subjects/auditable-user", headers=h)

    # The pseudonym is exposed (only) via the erasure audit entry.
    logs = client.get("/api/v1/audit-logs?limit=50", headers=a).json()
    delete_log = next(e for e in logs if e["action"] == "subject.delete")
    pseudonym = delete_log["detail"]["subject_pseudonym"]
    assert pseudonym and victim not in pseudonym

    # A read-only auditor can still inspect the retained pseudonymous history.
    r = client.get(
        f"/api/v1/history-by-pseudonym?pseudonym={pseudonym}&purpose_key=marketing",
        headers=a,
    )
    assert r.status_code == 200
    events = r.json()
    assert len(events) == 1
    assert events[0]["event_type"] == "grant"
    assert victim not in str(events)


def test_new_grant_after_erasure_starts_fresh_history_line(client, org, purpose, policy_v1):
    from app.db import SessionLocal
    from sqlalchemy import func, select

    from app.models import ConsentEvent

    h = org["admin"]
    client.post("/api/v1/events/grant",
                json={"event_id": _eid(), "expected_version": 0, "subject_ref": "reused",
                      "purpose_key": "marketing", "policy_version": 1}, headers=h)
    client.delete("/api/v1/subjects/reused", headers=h)

    # If the same natural person re-registers, the new incarnation is independent:
    # expected_version starts at 0 and the sequence starts at 1 again.
    r = client.post("/api/v1/events/grant",
                    json={"event_id": _eid(), "expected_version": 0,
                          "subject_ref": "reused", "purpose_key": "marketing",
                          "policy_version": 1}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["basis"]["event_sequence"] == 1

    db = SessionLocal()
    try:
        # Old pseudonymous history line retained + new incarnation line started.
        assert db.execute(
            select(func.count()).select_from(ConsentEvent)
            .where(ConsentEvent.purpose_key == "marketing")
        ).scalar_one() == 2
    finally:
        db.close()
