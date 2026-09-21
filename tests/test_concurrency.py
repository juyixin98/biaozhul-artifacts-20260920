"""Concurrency: atomic quota occupation and serialized nonce/idempotency keys.

These tests open REAL parallel sessions/threads against PostgreSQL so the
SELECT ... FOR UPDATE serialization and the unique indexes are exercised,
not merely mocked.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import SignRequest
from app import services
from tests.conftest import make_custodial, make_draft, register


@dataclass
class _Outcome:
    results: list = field(default_factory=list)  # (ok, status/code)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _worker(
    outcomes: _Outcome,
    user_id: str,
    draft_id: str,
    idem: str,
    barrier: threading.Barrier,
):
    db: Session = SessionLocal()
    try:
        barrier.wait(timeout=15)
        try:
            req, _replayed = services.submit_sign(
                db,
                user_id=user_id,
                draft_id=draft_id,
                idempotency_key=idem,
                request_id=f"concurrent-{idem}",
            )
            with outcomes.lock:
                outcomes.results.append(("ok", req.status, int(req.value_wei), idem))
        except Exception as exc:  # noqa: BLE001
            with outcomes.lock:
                outcomes.results.append(("err", getattr(exc, "code", str(exc)), 0, idem))
    finally:
        db.close()


def test_concurrent_quota_exactly_one_winner_per_wei(client):
    settings.daily_quota_wei = 1_000
    settings.cooldown_seconds = 0
    try:
        user_id, _key = register(client, "racer")
        w = make_custodial(client, _key)
        # Five drafts, each 300 wei, distinct nonces.
        drafts = [
            make_draft(client, _key, w["id"], nonce=100 + i, value=300)
            for i in range(5)
        ]
        outcomes = _Outcome()
        barrier = threading.Barrier(5)
        threads = [
            threading.Thread(
                target=_worker,
                args=(outcomes, user_id, drafts[i]["id"], f"idem-conc-{i:08d}", barrier),
            )
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        oks = [r for r in outcomes.results if r[0] == "ok" and r[1] == "success"]
        errs = [r for r in outcomes.results if r[0] == "err"]
        # Quota of 1000 allows exactly 3 * 300; the other 2 must be rejected.
        assert len(oks) == 3, outcomes.results
        assert len(errs) == 2
        assert {e[1] for e in errs} == {"daily_quota_exceeded"}
        assert sum(o[2] for o in oks) == 900

        # Persistent state agrees: exactly 3 occupying rows, no double debit.
        db = SessionLocal()
        try:
            occupying = db.query(SignRequest).filter(
                SignRequest.wallet_id == w["id"],
                SignRequest.status.in_(["pending", "success"]),
            ).all()
            assert len(occupying) == 3
            assert sum(int(r.value_wei) for r in occupying) == 900
        finally:
            db.close()
    finally:
        settings.daily_quota_wei = 10**18


def test_concurrent_same_nonce_only_one_succeeds(client):
    settings.daily_quota_wei = 10**18
    settings.cooldown_seconds = 0
    user_id, key = register(client, "racer2")
    w = make_custodial(client, key)
    drafts = [
        make_draft(client, key, w["id"], nonce=200, value=100 + i)
        for i in range(4)  # same nonce, DIFFERENT value content
    ]
    outcomes = _Outcome()
    barrier = threading.Barrier(4)
    threads = [
        threading.Thread(
            target=_worker,
            args=(outcomes, user_id, drafts[i]["id"], f"idem-noncec-{i:07d}", barrier),
        )
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    oks = [r for r in outcomes.results if r[0] == "ok"]
    errs = [r for r in outcomes.results if r[0] == "err"]
    assert len(oks) == 1, outcomes.results
    assert all(e[1] == "nonce_conflict" for e in errs), outcomes.results


def test_concurrent_duplicate_idempotency_key_returns_one_result(client):
    settings.cooldown_seconds = 0
    user_id, key = register(client, "racer3")
    w = make_custodial(client, key)
    d = make_draft(client, key, w["id"], nonce=300, value=7)
    outcomes = _Outcome()
    n = 4
    barrier = threading.Barrier(n)
    threads = [
        threading.Thread(
            target=_worker,
            args=(outcomes, user_id, d["id"], "idem-dup-0000000001", barrier),
        )
        for _ in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    # All callers succeed logically, but only ONE request row is produced.
    assert all(r[0] == "ok" and r[1] == "success" for r in outcomes.results), outcomes.results
    db = SessionLocal()
    try:
        rows = db.query(SignRequest).filter(
            SignRequest.idempotency_key == "idem-dup-0000000001"
        ).all()
        assert len(rows) == 1
        # Quota was occupied once.
        assert sum(int(r.value_wei) for r in rows) == 7
    finally:
        db.close()
