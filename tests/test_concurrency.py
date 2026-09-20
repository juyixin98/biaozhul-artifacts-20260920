"""Concurrency: parallel withdrawals/grants serialize; exactly one wins a version."""
from __future__ import annotations

import threading

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ConsentEvent, ConsentState
from app.schemas import GrantIn
from app import service


def _ensure_policy(org_id: int) -> None:
    db = SessionLocal()
    try:
        service.publish_policy(db, org_id, "v1")
    finally:
        db.close()


def test_concurrent_writes_same_version_only_one_wins(client, org):
    """Two writers that both believe expected_version=0: one applies, one 409s."""
    _ensure_policy(org["org_id"])
    results: list[Exception | object] = []
    lock = threading.Lock()

    def worker(event_id: str):
        db = SessionLocal()
        try:
            out = service.apply_event(
                db,
                org["org_id"],
                GrantIn(
                    event_id=event_id,
                    subject_key="user-c",
                    purpose="analytics",
                    action="grant",
                    expected_version=0,
                ),
            )
            with lock:
                results.append(out)
        except Exception as exc:  # noqa: BLE001
            with lock:
                results.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(f"evt-{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(conflicts) == 4
    from app.errors import Conflict

    assert all(isinstance(r, Conflict) for r in conflicts)

    # Ledger holds exactly the one winning event; state version is 1.
    db = SessionLocal()
    try:
        n_events = db.scalars(
            select(ConsentEvent).where(
                ConsentEvent.organization_id == org["org_id"],
                ConsentEvent.subject_key_snapshot == "user-c",
            )
        ).all()
        assert len(n_events) == 1
        state = db.scalars(
            select(ConsentState).where(
                ConsentState.organization_id == org["org_id"]
            )
        ).one()
        assert state.version == 1
        assert state.status == "granted"
    finally:
        db.close()


def test_concurrent_grant_and_withdraw_serialize(client, org):
    """Grant then a concurrent stale withdraw and a correct withdraw."""
    _ensure_policy(org["org_id"])
    db = SessionLocal()
    try:
        service.apply_event(
            db,
            org["org_id"],
            GrantIn(
                event_id="g1",
                subject_key="user-cw",
                purpose="p",
                action="grant",
                expected_version=0,
            ),
        )
    finally:
        db.close()

    out: list[object] = []
    lock = threading.Lock()

    def worker(event_id, expected):
        s = SessionLocal()
        try:
            try:
                r = service.apply_event(
                    s,
                    org["org_id"],
                    GrantIn(
                        event_id=event_id,
                        subject_key="user-cw",
                        purpose="p",
                        action="withdraw",
                        expected_version=expected,
                    ),
                )
                res = ("ok", r.state_version)
            except Exception as exc:  # noqa: BLE001
                res = ("conflict", type(exc).__name__)
            with lock:
                out.append(res)
        finally:
            s.close()

    # Both race: one thinks version is 1 (correct), one thinks 0 (stale).
    t1 = threading.Thread(target=worker, args=("w-correct", 1))
    t2 = threading.Thread(target=worker, args=("w-stale", 0))
    t1.start(); t2.start(); t1.join(); t2.join()

    oks = [r for r in out if r[0] == "ok"]
    errs = [r for r in out if r[0] == "conflict"]
    assert len(oks) == 1 and oks[0][1] == 2
    assert len(errs) == 1
