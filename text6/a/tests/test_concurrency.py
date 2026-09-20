"""Concurrency: OCC under parallel writers and rebuild-vs-write containment."""

import threading
import uuid
from datetime import datetime, timedelta, timezone


from app.db import SessionLocal
from app import services
from app.services_rebuild import rebuild_organization


def _eid():
    return f"evt-{uuid.uuid4()}"


def test_concurrent_withdrawals_exactly_one_wins(client, org, purpose, policy_v1):
    h = org["admin"]
    # Start from a granted state (version 1).
    client.post(
        "/api/v1/events/grant",
        json={"event_id": _eid(), "expected_version": 0, "subject_ref": "race",
              "purpose_key": "marketing", "policy_version": 1}, headers=h,
    )

    results: list[int] = []
    barrier = threading.Barrier(2)

    def withdraw(statuses: list[int], eid: str) -> None:
        db = SessionLocal()
        try:
            principal = services.Principal(
                organization_id=org["id"], role="admin", key_id=1)
            barrier.wait(timeout=10)
            try:
                services.write_event(
                    db, principal, event_id=eid, expected_version=1,
                    subject_ref="race", purpose_key="marketing",
                    action="withdrawal",
                )
                statuses.append(200)
            except services.Conflict as exc:
                db.rollback()
                statuses.append(exc.status_code)
        finally:
            db.close()

    t1 = threading.Thread(target=withdraw, args=(results, _eid()))
    t2 = threading.Thread(target=withdraw, args=(results, _eid()))
    t1.start(); t2.start()
    t1.join(timeout=20); t2.join(timeout=20)

    assert sorted(results) == [200, 409]

    body = client.get(
        "/api/v1/verify?subject_ref=race&purpose_key=marketing", headers=h
    ).json()
    assert body["valid"] is False
    assert body["state_version"] == 2
    # Exactly one withdrawal event in history.
    hist = client.get(
        "/api/v1/history?subject_ref=race&purpose_key=marketing", headers=h
    ).json()
    assert [e["event_type"] for e in hist] == ["grant", "withdrawal"]


def test_concurrent_grants_same_new_subject_one_wins(client, org, purpose, policy_v1):
    results = []
    barrier = threading.Barrier(2)

    def grant(eid: str) -> None:
        db = SessionLocal()
        try:
            principal = services.Principal(
                organization_id=org["id"], role="admin", key_id=1)
            barrier.wait(timeout=10)
            try:
                services.write_event(
                    db, principal, event_id=eid, expected_version=0,
                    subject_ref="fresh", purpose_key="marketing",
                    action="grant", policy_version=1,
                )
                results.append(200)
            except services.Conflict:
                db.rollback()
                results.append(409)
        finally:
            db.close()

    threads = [threading.Thread(target=grant, args=(_eid(),)) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=20)
    assert sorted(results) == [200, 409]


def test_rebuild_blocks_writers_and_no_event_is_lost(client, org, purpose, policy_v1):
    """A write committing while a rebuild runs is serialized by the org lock:
    either included in the replay or applied afterwards — final state must be
    exactly the union."""
    h = org["admin"]
    # Pre-existing grant for subject 'base'.
    client.post(
        "/api/v1/events/grant",
        json={"event_id": _eid(), "expected_version": 0, "subject_ref": "base",
              "purpose_key": "marketing", "policy_version": 1}, headers=h,
    )

    errors = []

    def writer() -> None:
        # Try to write while the rebuild transaction is open; the org advisory
        # lock parks this until rebuild commits.
        db = SessionLocal()
        try:
            principal = services.Principal(
                organization_id=org["id"], role="admin", key_id=1)
            services.write_event(
                db, principal, event_id=_eid(), expected_version=1,
                subject_ref="base", purpose_key="marketing",
                action="withdrawal",
            )
        except Exception as exc:  # pragma: no cover - failure diagnostic
            db.rollback()
            errors.append(exc)
        finally:
            db.close()

    db = SessionLocal()
    try:
        rebuild_db = SessionLocal()
        try:
            # Acquire the org lock + start rebuilding, but do NOT commit yet.
            from sqlalchemy import BigInteger, cast, func, select
            from app.locking import ADVISORY_ORG_PREFIX
            rebuild_db.execute(
                select(func.pg_advisory_xact_lock(
                    cast(func.hashtextextended(
                        f"{ADVISORY_ORG_PREFIX}:{org['id']}", 0), BigInteger)))
            )
            t = threading.Thread(target=writer)
            t.start()
            # Give the writer time to block on the lock.
            t.join(timeout=2)
            assert t.is_alive(), "writer should be blocked by the rebuild lock"

            result = rebuild_organization(rebuild_db, org["id"])
            # Rebuild replays only the pre-existing grant (writer still blocked).
            assert result["replayed_events"] == 1
            rebuild_db.commit()  # release lock

            t.join(timeout=20)
            assert not t.is_alive()
        finally:
            rebuild_db.close()
    finally:
        db.close()

    assert not errors, errors
    # Writer applied after rebuild: state withdrawn at version 2, history intact.
    body = client.get(
        "/api/v1/verify?subject_ref=base&purpose_key=marketing", headers=h
    ).json()
    assert body["valid"] is False
    assert body["state_version"] == 2
    hist = client.get(
        "/api/v1/history?subject_ref=base&purpose_key=marketing", headers=h
    ).json()
    assert [e["event_type"] for e in hist] == ["grant", "withdrawal"]


def test_soon_expiring_grant_boundary_under_concurrent_verify(
    client, org, purpose, policy_v1
):
    """Expiry never depends on writers: verifies around the boundary agree."""
    import time
    h = org["admin"]
    expires = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    client.post(
        "/api/v1/events/grant",
        json={"event_id": _eid(), "expected_version": 0, "subject_ref": "tick",
              "purpose_key": "marketing", "policy_version": 1,
              "expires_at": expires}, headers=h,
    )
    time.sleep(1.2)
    outcomes = set()

    def check() -> None:
        body = client.get(
            "/api/v1/verify?subject_ref=tick&purpose_key=marketing", headers=h
        ).json()
        outcomes.add(body["valid"])

    threads = [threading.Thread(target=check) for _ in range(4)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=10)
    assert outcomes == {False}
