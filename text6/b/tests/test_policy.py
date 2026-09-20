"""Policy immutability and non-renewal: publishing a new version never
modifies an existing version nor renews grants bound to the old one.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tests.conftest import create_subject, grant


def test_published_policy_cannot_be_modified(client, admin_headers, db):
    r = client.post(
        "/v1/policies",
        headers=admin_headers,
        json={"version": "v2026-09", "body": "original"},
    )
    assert r.status_code == 201, r.text

    # Application level: re-publishing the same version label is a conflict.
    r2 = client.post(
        "/v1/policies",
        headers=admin_headers,
        json={"version": "v2026-09", "body": "rewrite attempt"},
    )
    assert r2.status_code == 409

    # Database level: a raw UPDATE is rejected by the append-only trigger.
    with db.bind.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(
                text("UPDATE policy_versions SET body = 'hacked' WHERE version = 'v2026-09'")
            )
            trans.commit()
            raised = False
        except Exception:
            raised = True
            trans.rollback()
    assert raised, "database trigger must forbid UPDATE on policy_versions"


def test_consent_events_cannot_be_deleted(client, admin_headers, db):
    subject = create_subject(client, admin_headers)
    r, _ = grant(
        client, admin_headers, subject["id"], "immutable",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    assert r.status_code == 200

    # A direct DELETE against the ledger must be rejected by the trigger.
    with db.bind.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("DELETE FROM consent_events WHERE id >= 0"))
            trans.commit()
            raised = False
        except Exception:
            raised = True
            trans.rollback()
    assert raised, "database trigger must forbid DELETE on consent_events"


def test_new_policy_does_not_renew_old_grant(client, admin_headers):
    subject = create_subject(client, admin_headers)
    purpose = "profiling"
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    r, _ = grant(
        client, admin_headers, subject["id"], purpose,
        policy_version="v1", expires_at=expiry,
    )
    assert r.status_code == 200

    # Publish v2; the existing grant remains bound to v1.
    pub = client.post(
        "/v1/policies",
        headers=admin_headers,
        json={"version": "v99", "body": "shiny new policy"},
    )
    assert pub.status_code == 201

    verify = client.get(
        f"/v1/consent/{subject['id']}/{purpose}/verify", headers=admin_headers
    ).json()
    assert verify["valid"] is True
    assert verify["policy_version"] == "v1"

    history = client.get(
        f"/v1/subjects/{subject['id']}/history", headers=admin_headers
    ).json()
    assert history[0]["policy_version"] == "v1"


def test_grant_must_reference_published_version(client, admin_headers):
    subject = create_subject(client, admin_headers)
    r, _ = grant(
        client, admin_headers, subject["id"], "x",
        policy_version="does-not-exist",
        event_id=f"evt-{uuid.uuid4().hex[:8]}",
    )
    assert r.status_code == 422
    # Failed event must not enter history.
    history = client.get(
        f"/v1/subjects/{subject['id']}/history", headers=admin_headers
    ).json()
    assert history == []
