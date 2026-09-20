"""Expiry is evaluated at read time — no cleanup job required."""

import time
import uuid
from datetime import datetime, timedelta, timezone


def _eid():
    return f"evt-{uuid.uuid4()}"


def test_consent_valid_before_expiry_and_invalid_after(client, org, purpose, policy_v1):
    h = org["admin"]
    boundary = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    r = client.post(
        "/api/v1/events/grant",
        json={"event_id": _eid(), "expected_version": 0, "subject_ref": "exp",
              "purpose_key": "marketing", "policy_version": 1,
              "expires_at": boundary},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["valid"] is True

    # Sleep past the boundary; verify immediately reports expiry.
    time.sleep(1.2)
    r = client.get("/api/v1/verify?subject_ref=exp&purpose_key=marketing", headers=h)
    body = r.json()
    assert body["valid"] is False
    assert body["reason"] == "expired"
    # The grant evidence is still available for the audit question "why invalid".
    assert body["basis"]["grant_event_sequence"] == 1


def test_exactly_at_expiry_is_invalid(client, org, purpose, policy_v1):
    """Boundary rule: expires_at <= now => invalid (inclusive)."""
    h = org["admin"]
    # A grant that expires essentially immediately.
    boundary = (datetime.now(timezone.utc) + timedelta(milliseconds=300)).isoformat()
    client.post(
        "/api/v1/events/grant",
        json={"event_id": _eid(), "expected_version": 0, "subject_ref": "edge",
              "purpose_key": "marketing", "policy_version": 1,
              "expires_at": boundary},
        headers=h,
    )
    time.sleep(0.5)
    r = client.get("/api/v1/verify?subject_ref=edge&purpose_key=marketing", headers=h)
    assert r.json()["valid"] is False
    assert r.json()["reason"] == "expired"


def test_grant_already_expired_rejected(client, org, purpose, policy_v1):
    h = org["admin"]
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    r = client.post(
        "/api/v1/events/grant",
        json={"event_id": _eid(), "expected_version": 0, "subject_ref": "late",
              "purpose_key": "marketing", "policy_version": 1,
              "expires_at": past},
        headers=h,
    )
    assert r.status_code == 422
    assert r.json()["error"] == "already_expired"
    # Nothing persisted.
    r = client.get("/api/v1/verify?subject_ref=late&purpose_key=marketing", headers=h)
    assert r.json()["status"] == "no_record"


def test_non_expiring_grant_stays_valid(client, org, purpose, policy_v1):
    h = org["admin"]
    client.post(
        "/api/v1/events/grant",
        json={"event_id": _eid(), "expected_version": 0, "subject_ref": "forever",
              "purpose_key": "marketing", "policy_version": 1},
        headers=h,
    )
    r = client.get("/api/v1/verify?subject_ref=forever&purpose_key=marketing", headers=h)
    assert r.json()["valid"] is True
    assert r.json()["grant"]["expires_at"] is None
