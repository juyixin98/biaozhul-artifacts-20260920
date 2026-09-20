"""Subject erasure, role separation and organization isolation."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.models import AuditLog, ExportCopy, Subject
from tests.conftest import create_subject, grant


def test_erasure_clears_mapping_and_exports_keeps_pii_free_history(
    client, admin_headers, auditor_headers, db
):
    subject = create_subject(
        client, admin_headers, external_ref="victim-ref", email="x@y.z",
        display_name="X Y",
    )
    grant(
        client, admin_headers, subject["id"], "marketing",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    client.post(
        f"/v1/subjects/{subject['id']}/exports",
        headers=admin_headers,
        json={"subject_id": subject["id"], "destination": "dsr",
              "payload": "email=x@y.z"},
    )

    r = client.post(
        f"/v1/subjects/{subject['id']}/erase", headers=admin_headers
    )
    assert r.status_code == 200, r.text
    tombstone = r.json()
    assert tombstone["erased"] is True
    assert tombstone["external_ref"] is None
    assert tombstone["email"] is None
    assert tombstone["display_name"] is None
    assert tombstone["erased_at"] is not None

    db.expire_all()
    row = db.query(Subject).filter(Subject.id == subject["id"]).one()
    assert row.external_ref is None and row.email is None and row.display_name is None
    assert (
        db.query(ExportCopy).filter(ExportCopy.subject_id == subject["id"]).count()
        == 0
    )

    # Resolution by the (now destroyed) external mapping returns 404.
    r_ref = client.get("/v1/subjects/by-ref/victim-ref", headers=auditor_headers)
    assert r_ref.status_code == 404

    # History ledger survives, referenced by opaque id, with no personal data.
    history = client.get(
        f"/v1/subjects/{subject['id']}/history", headers=auditor_headers
    ).json()
    assert len(history) == 1
    assert all("email" not in str(ev) for ev in history)

    # Verification on the internal id still yields a recorded verdict.
    verdict = client.get(
        f"/v1/consent/{subject['id']}/marketing/verify", headers=auditor_headers
    ).json()
    assert verdict["current_version"] == 1


def test_erased_subject_rejects_new_events(client, admin_headers):
    subject = create_subject(client, admin_headers)
    client.post(f"/v1/subjects/{subject['id']}/erase", headers=admin_headers)
    r, _ = grant(client, admin_headers, subject["id"], "p")
    assert r.status_code == 410


def test_double_erase_is_idempotent_gone(client, admin_headers):
    subject = create_subject(client, admin_headers)
    client.post(f"/v1/subjects/{subject['id']}/erase", headers=admin_headers)
    r = client.post(f"/v1/subjects/{subject['id']}/erase", headers=admin_headers)
    assert r.status_code == 410


def test_audit_log_has_no_personal_fields(client, admin_headers, auditor_headers, db):
    create_subject(
        client, admin_headers, external_ref="pii-ref",
        email="personal@example.com", display_name="Personal Name",
    )
    logs = client.get("/v1/audit-logs?limit=50", headers=auditor_headers).json()
    assert any(log["action"] == "subject_created" for log in logs)
    blob = repr(logs)
    assert "personal@example.com" not in blob
    assert "Personal Name" not in blob
    assert "pii-ref" not in blob

    # Database spot-check: detail JSON only carries the opaque subject id.
    db.expire_all()
    entry = (
        db.query(AuditLog).filter(AuditLog.action == "subject_created").one()
    )
    assert set(entry.detail.keys()) == {"subject_id"}


def test_auditor_is_read_only(client, auditor_headers):
    assert client.post(
        "/v1/policies", headers=auditor_headers,
        json={"version": "x", "body": "y"},
    ).status_code == 403
    assert client.post(
        "/v1/consent/batch", headers=auditor_headers, json={"items": []}
    ).status_code in (403, 422)
    assert client.post(
        "/v1/admin/rebuild", headers=auditor_headers
    ).status_code == 403
    assert client.post(
        "/v1/subjects", headers=auditor_headers,
        json={"external_ref": "z"},
    ).status_code == 403
    # Reads are allowed.
    assert client.get("/v1/policies", headers=auditor_headers).status_code == 200
    assert client.get("/v1/audit-logs", headers=auditor_headers).status_code == 200


def test_missing_and_bad_api_keys(client):
    assert client.get("/v1/policies").status_code == 401
    assert client.get(
        "/v1/policies", headers={"X-API-Key": "nonsense"}
    ).status_code == 401


def test_organization_isolation(client, admin_headers, org_b_headers):
    subject = create_subject(client, admin_headers)
    # Org B cannot read, verify, or write against org A's subject.
    assert client.get(
        f"/v1/subjects/{subject['id']}", headers=org_b_headers
    ).status_code == 404
    assert client.get(
        f"/v1/consent/{subject['id']}/whatever/verify", headers=org_b_headers
    ).status_code == 404
    r, _ = grant(client, org_b_headers, subject["id"], "p",
                 event_id=f"e-{uuid.uuid4().hex[:8]}")
    assert r.status_code == 404

    # Org B also cannot see org A's published policies / audit entries.
    policies = client.get("/v1/policies", headers=org_b_headers).json()
    assert all(p["body"] == "org b policy" for p in policies)
    audit = client.get("/v1/audit-logs", headers=org_b_headers).json()
    assert audit == []


def test_query_results_after_erasure(client, admin_headers, auditor_headers):
    """Cover the full post-erasure query matrix."""
    subject = create_subject(
        client, admin_headers, external_ref="erase-me-ref"
    )
    grant(
        client, admin_headers, subject["id"], "sales",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    client.post(f"/v1/subjects/{subject['id']}/erase", headers=admin_headers)

    # Internal id fetch: tombstone visible.
    assert client.get(
        f"/v1/subjects/{subject['id']}", headers=auditor_headers
    ).json()["erased"] is True

    # External lookup: 404 as if the subject never existed for that mapping.
    assert client.get(
        "/v1/subjects/by-ref/erase-me-ref", headers=auditor_headers
    ).status_code == 404

    # Verify still works against the opaque internal id (ledger retained).
    v = client.get(
        f"/v1/consent/{subject['id']}/sales/verify", headers=auditor_headers
    ).json()
    assert v["valid"] is True
    assert v["grant_event_id"] is not None
