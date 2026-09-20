"""Expiry boundary: consent is valid up to (but not including) the expiry
instant, invalid at/after it, with no cleanup task involved.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.consent import verify_consent
from tests.conftest import create_subject, grant


def test_expiry_boundary_service_level(db, admin_headers, client):
    subject = create_subject(client, admin_headers)
    purpose = "cookies"
    expiry = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    r, _ = grant(client, admin_headers, subject["id"], purpose, expires_at=expiry)
    assert r.status_code == 200, r.text

    # One microsecond before expiry: valid.
    res = verify_consent(
        db,
        organization_id=_org(db),
        subject_id=subject["id"],
        purpose=purpose,
        now=expiry - timedelta(microseconds=1),
    )
    assert res["valid"] is True
    assert res["reason"] == "valid"

    # Exactly at the expiry instant: expired (>= comparison).
    res_at = verify_consent(
        db,
        organization_id=_org(db),
        subject_id=subject["id"],
        purpose=purpose,
        now=expiry,
    )
    assert res_at["valid"] is False
    assert res_at["reason"] == "expired"

    # Well after: still expired, basis event still reported.
    res_after = verify_consent(
        db,
        organization_id=_org(db),
        subject_id=subject["id"],
        purpose=purpose,
        now=expiry + timedelta(days=400),
    )
    assert res_after["valid"] is False
    assert res_after["grant_event_id"] is not None
    assert res_after["policy_version"] == "v1"


def test_expiry_no_cleanup_required(client, admin_headers):
    """A short-lived grant flips to invalid purely via read-time evaluation."""
    subject = create_subject(client, admin_headers)
    purpose = "flash"
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    r, _ = grant(client, admin_headers, subject["id"], purpose, expires_at=expires_at)
    assert r.status_code == 200
    assert client.get(
        f"/v1/consent/{subject['id']}/{purpose}/verify", headers=admin_headers
    ).json()["valid"] is True

    import time

    time.sleep(1.1)
    verdict = client.get(
        f"/v1/consent/{subject['id']}/{purpose}/verify", headers=admin_headers
    ).json()
    assert verdict["valid"] is False
    assert verdict["reason"] == "expired"
    # No state row mutation / no janitor: history length is still one.
    history = client.get(
        f"/v1/subjects/{subject['id']}/history", headers=admin_headers
    ).json()
    assert len(history) == 1


def _org(db):
    from app.models import Organization

    return db.query(Organization).filter(Organization.name == "Org A").one().id
