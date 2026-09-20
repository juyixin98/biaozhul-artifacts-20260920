"""Idempotency key semantics and optimistic version conflicts."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from tests.conftest import create_subject, grant


def test_same_request_returns_original_result(client, admin_headers):
    subject = create_subject(client, admin_headers)
    purpose = "emails"
    expiry = datetime.now(timezone.utc) + timedelta(days=3)
    event_id = f"evt-{uuid.uuid4().hex[:10]}"

    r1, _ = grant(
        client, admin_headers, subject["id"], purpose,
        event_id=event_id, expires_at=expiry,
    )
    assert r1.status_code == 200
    first = r1.json()
    assert first["replayed"] is False

    r2, _ = grant(
        client, admin_headers, subject["id"], purpose,
        event_id=event_id, expires_at=expiry,
    )
    assert r2.status_code == 200
    second = r2.json()
    assert second["replayed"] is True
    assert second["version"] == first["version"]
    assert second["created_at"] == first["created_at"]

    # Exactly one ledger row.
    history = client.get(
        f"/v1/subjects/{subject['id']}/history", headers=admin_headers
    ).json()
    assert len(history) == 1


def test_same_event_id_different_content_conflicts(client, admin_headers):
    subject = create_subject(client, admin_headers)
    event_id = f"evt-{uuid.uuid4().hex[:10]}"
    r1, _ = grant(
        client, admin_headers, subject["id"], "purpose-a",
        event_id=event_id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    assert r1.status_code == 200

    # Same event id, different purpose -> 409 conflict.
    r2, _ = grant(
        client, admin_headers, subject["id"], "purpose-b",
        event_id=event_id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    assert r2.status_code == 409
    assert r2.json()["error"] == "conflict"


def test_stale_expected_version_conflicts(client, admin_headers):
    subject = create_subject(client, admin_headers)
    purpose = "geo"
    r1, _ = grant(
        client, admin_headers, subject["id"], purpose,
        expected_version=0,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    assert r1.status_code == 200

    # A second grant on an active stream fails semantically; but a withdraw at
    # a stale version (0 instead of 1) is a version conflict.
    r2 = client.post(
        "/v1/consent/withdraw",
        headers=admin_headers,
        json={
            "event_id": f"w-{uuid.uuid4().hex[:10]}",
            "subject_id": subject["id"],
            "purpose": purpose,
            "expected_version": 0,
        },
    )
    assert r2.status_code == 409
    assert "version" in r2.json()["detail"]

    # Stream untouched by the failed attempt.
    verify = client.get(
        f"/v1/consent/{subject['id']}/{purpose}/verify", headers=admin_headers
    ).json()
    assert verify["valid"] is True
    assert verify["current_version"] == 1


def test_failed_validation_writes_nothing(client, admin_headers):
    subject = create_subject(client, admin_headers)
    # Withdraw on a stream that never existed.
    r = client.post(
        "/v1/consent/withdraw",
        headers=admin_headers,
        json={
            "event_id": f"w-{uuid.uuid4().hex[:10]}",
            "subject_id": subject["id"],
            "purpose": "ghost",
            "expected_version": 0,
        },
    )
    assert r.status_code == 422
    assert (
        client.get(
            f"/v1/subjects/{subject['id']}/history", headers=admin_headers
        ).json()
        == []
    )


def test_event_id_reused_on_different_stream_conflicts(client, admin_headers):
    s1 = create_subject(client, admin_headers)
    s2 = create_subject(client, admin_headers)
    shared_id = f"evt-{uuid.uuid4().hex[:10]}"
    expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    r1, _ = grant(
        client, admin_headers, s1["id"], "purpose-a",
        event_id=shared_id, expires_at=expiry,
    )
    assert r1.status_code == 200

    # Same event id reused for an otherwise different subject: content differs
    # (subject id is in the fingerprint), so this is a 409 either way.
    r2, _ = grant(
        client, admin_headers, s2["id"], "purpose-a",
        event_id=shared_id, expires_at=expiry,
    )
    assert r2.status_code == 409
