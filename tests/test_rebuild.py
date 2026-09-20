"""Rebuild from immutable history: identical to incremental view; no events lost."""
from __future__ import annotations

import threading

from sqlalchemy import select, text

from app.db import SessionLocal, engine
from app.models import ConsentEvent, ConsentState
from app.schemas import GrantIn
from app import service
from tests.conftest import auth


def _seed_mix(org_id: int):
    db = SessionLocal()
    try:
        service.publish_policy(db, org_id, "v1")
        for subject in ("a", "b", "c"):
            service.apply_event(
                db,
                org_id,
                GrantIn(
                    event_id=f"{subject}-g",
                    subject_key=subject,
                    purpose="analytics",
                    action="grant",
                    expected_version=0,
                ),
            )
        service.apply_event(
            db,
            org_id,
            GrantIn(
                event_id="a-w",
                subject_key="a",
                purpose="analytics",
                action="withdraw",
                expected_version=1,
            ),
        )
    finally:
        db.close()


def _state_snapshot(org_id: int) -> dict:
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(ConsentState).where(ConsentState.organization_id == org_id)
        ).all()
        return {
            (r.subject_id, r.purpose): (
                r.status,
                r.version,
                r.granted_event_id,
                r.latest_event_id,
                r.policy_version,
            )
            for r in rows
        }
    finally:
        db.close()


def test_rebuild_matches_incremental_view(client, org):
    _seed_mix(org["org_id"])
    before = _state_snapshot(org["org_id"])

    r = client.post("/admin/rebuild", headers=auth(org["admin"]))
    assert r.status_code == 200, r.text
    stats = r.json()
    assert stats["replayed_events"] == 4
    assert stats["materialized_states"] == 3

    after = _state_snapshot(org["org_id"])
    assert before == after


def test_rebuild_repairs_corrupted_materialisation(client, org):
    """Wipe the derived table, then rebuild — it must be fully reconstructed."""
    _seed_mix(org["org_id"])
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM consent_states"))
    assert _state_snapshot(org["org_id"]) == {}

    r = client.post("/admin/rebuild", headers=auth(org["admin"]))
    assert r.status_code == 200
    assert r.json()["materialized_states"] == 3

    # 'a' was withdrawn; that survives rebuild.
    state = client.get("/subjects/a/consents/analytics", headers=auth(org["admin"])).json()
    assert state["status"] == "withdrawn" and state["valid"] is False


def test_event_inserted_while_rebuild_lock_held_is_not_lost(org):
    """Direct lock-level proof of the no-loss guarantee.

    Hold an EXCLUSIVE lock on the ledger (as the rebuild does) in one
    transaction. In a second session, append an event: the INSERT blocks until
    the lock is released, then commits. The event is durably present and a
    subsequent rebuild reconciles state to the ledger exactly.
    """
    import time

    from app.models import Subject

    _seed_mix(org["org_id"])  # subject b holds one grant -> version 1

    locker = SessionLocal()
    writer = SessionLocal()
    try:
        # Session 1: take the rebuild lock inside an open transaction.
        locker.execute(text("LOCK TABLE consent_events IN EXCLUSIVE MODE"))

        result: dict[str, object] = {}

        def blocked_insert():
            try:
                service.apply_event(
                    writer,
                    org["org_id"],
                    GrantIn(
                        event_id="race-blocked",
                        subject_key="b",
                        purpose="analytics",
                        action="grant",
                        expected_version=1,
                    ),
                )
                result["ok"] = True
            except Exception as exc:  # noqa: BLE001
                result["error"] = type(exc).__name__

        th = threading.Thread(target=blocked_insert)
        th.start()
        time.sleep(1.0)  # let the writer reach the lock and block on it
        assert th.is_alive(), "writer should be blocked behind the rebuild lock"

        # While the writer is blocked, the event must not be visible yet.
        check = SessionLocal()
        try:
            visible = check.scalars(
                select(ConsentEvent).where(ConsentEvent.event_id == "race-blocked")
            ).one_or_none()
            assert visible is None
        finally:
            check.close()

        # Release the rebuild lock exactly like a completing rebuild would.
        locker.commit()
        th.join(timeout=10)
        assert not th.is_alive()
        assert result.get("ok") is True, result
    finally:
        writer.close()
        locker.close()

    # The blocked event landed exactly once; reconcile with a rebuild.
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(ConsentEvent).where(ConsentEvent.event_id == "race-blocked")
        ).all()
        assert len(rows) == 1
        b_subj = db.scalars(
            select(Subject).where(
                Subject.organization_id == org["org_id"], Subject.subject_key == "b"
            )
        ).one()
        state = db.scalars(
            select(ConsentState).where(ConsentState.subject_id == b_subj.id)
        ).one()
        assert state.version == 2
        assert state.latest_event_id == "race-blocked"
    finally:
        db.close()

    from app.replay import rebuild_organization

    db = SessionLocal()
    try:
        stats = rebuild_organization(db, org["org_id"])
        db.commit()
        b_subj = db.scalars(
            select(Subject).where(
                Subject.organization_id == org["org_id"], Subject.subject_key == "b"
            )
        ).one()
        state = db.scalars(
            select(ConsentState).where(ConsentState.subject_id == b_subj.id)
        ).one()
        # Rebuild reproduces the post-block state identically (no loss/dup).
        assert state.version == 2
        assert state.latest_event_id == "race-blocked"
        assert stats["materialized_states"] == 3
    finally:
        db.close()
