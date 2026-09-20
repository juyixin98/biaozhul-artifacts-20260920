"""Expiry boundary: invalid at exactly expires_at, with no cleanup job."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ConsentState
from app import service
from app.schemas import GrantIn


def _grant_expiring(org_id: int, expires_at):
    db = SessionLocal()
    try:
        service.publish_policy(db, org_id, "v1")
        service.apply_event(
            db,
            org_id,
            GrantIn(
                event_id="g-exp",
                subject_key="user-exp",
                purpose="analytics",
                action="grant",
                expected_version=0,
                expires_at=expires_at,
            ),
        )
    finally:
        db.close()


def test_valid_before_expiry_invalid_at_and_after(client, org):
    from app.models import utcnow

    deadline = utcnow().replace(microsecond=0) + timedelta(hours=1)
    _grant_expiring(org["org_id"], deadline)

    db = SessionLocal()
    try:
        # One second before -> valid.
        before = service.evaluate_consent(
            db, org["org_id"], "user-exp", "analytics", at=deadline - timedelta(seconds=1)
        )
        assert before["valid"] is True
        assert before["status"] == "granted"

        # Exactly at the boundary -> invalid (expires_at <= now).
        exact = service.evaluate_consent(
            db, org["org_id"], "user-exp", "analytics", at=deadline
        )
        assert exact["valid"] is False
        assert exact["reason"] == "grant has expired"
        assert exact["basis_event_id"] == "g-exp"

        # After -> invalid.
        after = service.evaluate_consent(
            db, org["org_id"], "user-exp", "analytics", at=deadline + timedelta(seconds=10)
        )
        assert after["valid"] is False
    finally:
        db.close()


def test_expiry_requires_no_cleanup_and_state_row_unchanged(client, org):
    """The materialised row still says granted; expiry is purely query-time."""
    from app.models import utcnow

    past = utcnow() - timedelta(seconds=5)
    _grant_expiring(org["org_id"], past)

    db = SessionLocal()
    try:
        evaled = service.evaluate_consent(
            db, org["org_id"], "user-exp", "analytics"
        )
        assert evaled["valid"] is False

        state = db.scalars(
            select(ConsentState).where(
                ConsentState.organization_id == org["org_id"]
            )
        ).one()
        # No sweeper changed the row; the query did the work.
        assert state.status == "granted"
        assert state.expires_at is not None and state.expires_at <= utcnow()
    finally:
        db.close()
