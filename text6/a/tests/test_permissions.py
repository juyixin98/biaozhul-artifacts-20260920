"""Role separation, organization isolation and immutability enforcement."""

import uuid

import pytest
from sqlalchemy import text

from app.db import SessionLocal


def _eid():
    return f"evt-{uuid.uuid4()}"


def test_auditor_is_read_only(client, org, purpose, policy_v1):
    a = org["auditor"]
    # Reads allowed.
    assert client.get("/api/v1/purposes", headers=a).status_code == 200
    assert client.get(
        "/api/v1/verify?subject_ref=x&purpose_key=marketing", headers=a
    ).status_code == 200
    assert client.get("/api/v1/audit-logs", headers=a).status_code == 200
    # Writes forbidden.
    r = client.post("/api/v1/purposes", json={"key": "x"}, headers=a)
    assert r.status_code == 403
    r = client.post(
        "/api/v1/events/grant",
        json={"event_id": _eid(), "expected_version": 0, "subject_ref": "x",
              "purpose_key": "marketing", "policy_version": 1}, headers=a,
    )
    assert r.status_code == 403
    r = client.post("/api/v1/rebuild", headers=a)
    assert r.status_code == 403
    r = client.delete("/api/v1/subjects/x", headers=a)
    assert r.status_code == 403


def test_organizations_cannot_read_each_others_data(client, make_org, org, purpose, policy_v1):
    org1 = org  # has purpose + grant via fixtures
    org2 = make_org()
    h1 = org1["admin"]
    h2 = org2["admin"]

    client.post(
        "/api/v1/events/grant",
        json={"event_id": _eid(), "expected_version": 0, "subject_ref": "secret",
              "purpose_key": "marketing", "policy_version": 1}, headers=h1,
    )

    # org2 has no such purpose -> not found, never leaks org1 data.
    r = client.get("/api/v1/verify?subject_ref=secret&purpose_key=marketing", headers=h2)
    assert r.status_code == 404
    r = client.get("/api/v1/purposes", headers=h2)
    assert r.json() == []
    # Even guessing the subject ref + purpose returns nothing.
    r = client.get("/api/v1/audit-logs", headers=h2)
    assert r.json() == []


def test_missing_and_invalid_keys_are_unauthorized(client):
    assert client.get("/api/v1/purposes").status_code == 401
    assert client.get("/api/v1/purposes", headers={"X-API-Key": "nope"}).status_code == 401


def test_management_key_required_for_org_creation(client):
    r = client.post("/management/organizations", json={"name": "x"})
    assert r.status_code == 401
    r = client.post(
        "/management/organizations", json={"name": "x"},
        headers={"X-Management-Key": "wrong"},
    )
    assert r.status_code == 401


def test_consent_events_table_is_immutable(client, org, purpose, policy_v1):
    h = org["admin"]
    client.post(
        "/api/v1/events/grant",
        json={"event_id": _eid(), "expected_version": 0, "subject_ref": "u",
              "purpose_key": "marketing", "policy_version": 1}, headers=h,
    )
    db = SessionLocal()
    try:
        with pytest.raises(Exception) as ei:
            db.execute(text("UPDATE consent_events SET event_type='withdrawal' WHERE id=1"))
            db.commit()
        db.rollback()
        assert "immutable" in str(ei.value).lower()

        with pytest.raises(Exception) as ei:
            db.execute(text("DELETE FROM consent_events"))
            db.commit()
        db.rollback()
        assert "immutable" in str(ei.value).lower()
    finally:
        db.close()


def test_policy_versions_table_is_immutable(client, org, purpose, policy_v1):
    db = SessionLocal()
    try:
        with pytest.raises(Exception) as ei:
            db.execute(text("UPDATE policy_versions SET body='tampered' WHERE version=1"))
            db.commit()
        db.rollback()
        assert "immutable" in str(ei.value).lower()
    finally:
        db.close()


def test_audit_logs_contain_no_personal_fields(client, org, purpose, policy_v1):
    h = org["admin"]
    victim = f"person-{uuid.uuid4().hex[:8]}"
    client.post(
        "/api/v1/events/grant",
        json={"event_id": _eid(), "expected_version": 0, "subject_ref": victim,
              "purpose_key": "marketing", "policy_version": 1}, headers=h,
    )
    client.delete(f"/api/v1/subjects/{victim}", headers=h)
    rows = client.get("/api/v1/audit-logs?limit=50", headers=h).json()
    assert rows, "expected audit entries"
    for row in rows:
        assert victim not in str(row["detail"])
        assert row["actor_role"] in ("admin", "auditor")
