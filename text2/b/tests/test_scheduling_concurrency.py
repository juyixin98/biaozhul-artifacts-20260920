"""Invitation lifecycle: 8-minute expiry, re-allocation, accept races.

These tests use *real threads and transactions* against PostgreSQL so the
row locks (SELECT ... FOR UPDATE) and partial unique indexes are exercised
exactly as in production.
"""
from __future__ import annotations

from datetime import timedelta
from threading import Barrier, Thread

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import FakeClock
from app.db import get_engine
from app.enums import AssignmentStatus, TaskStatus
from app.models import Assignment, Task
from app.services import scheduling
from app.services.errors import ConflictError
from app.services.generation import generate_tasks


def _setup_task(db, factory, clock, *, num_workers=3, task_index=0):
    unit = factory.unit(tz="UTC")
    bls = factory.qualification(code="BLS")
    workers = [factory.worker(external_id=f"w{i}", units=[unit]) for i in range(num_workers)]
    for w in workers:
        factory.grant(w, bls)
    plan = factory.plan(
        unit,
        timezone="UTC",
        period_days=1,
        slots=[
            {
                "weekday": d,
                "start_at": "09:00",
                "latest_start_at": "12:00",
                "duration_minutes": 60,
            }
            for d in range(7)
        ],
        qualification_ids=[bls.id],
    )
    tasks = generate_tasks(db, plan_id=plan.id, now=clock.now())
    db.commit()
    return tasks[task_index], workers


def test_invitation_expires_after_8_minutes_and_reallocates(db, factory, clock):
    task, workers = _setup_task(db, factory, clock)

    result = scheduling.schedule_task(db, clock, task.id)
    assert result.status == "invited"
    first_worker = result.worker_id
    db.commit()

    # Not yet expired at 7:59.
    clock.advance(minutes=7, seconds=59)
    with Session(get_engine()) as s:
        assert scheduling.schedule_task(s, clock, task.id).status == "already_live"
        s.rollback()

    # At 8:00 the invitation is dead; sweep re-invites the next candidate.
    clock.advance(seconds=1)
    with Session(get_engine()) as s:
        results = scheduling.sweep_and_reallocate(s, clock)
        s.commit()
    result2 = [r for r in results if r.task_id == task.id]
    assert result2 and result2[0].status == "invited"
    assert result2[0].worker_id != first_worker

    with Session(get_engine()) as s:
        task = s.get(Task, task.id)
        assert task.status == TaskStatus.INVITED.value
        statuses = [
            a.status.value
            for a in s.scalars(
                select(Assignment).where(Assignment.task_id == task.id)
            )
        ]
        assert statuses.count("pending") == 1
        assert statuses.count("expired") == 1
        assert "accepted" not in statuses


def test_late_accept_is_rejected_and_does_not_hold_hours(db, factory, clock):
    task, workers = _setup_task(db, factory, clock, num_workers=2)
    result = scheduling.schedule_task(db, clock, task.id)
    asm_id = result.assignment_id
    db.commit()

    clock.advance(minutes=8, seconds=1)
    with Session(get_engine()) as s:
        with pytest.raises(ConflictError) as exc:
            scheduling.accept_invitation(s, clock, asm_id)
        assert exc.value.message.startswith("Invitation expired")
        s.commit()

    with Session(get_engine()) as s:
        asm = s.get(Assignment, asm_id)
        assert asm.status == AssignmentStatus.EXPIRED.value
        task = s.get(Task, task.id)
        assert task.status == TaskStatus.PENDING.value


def test_concurrent_accepts_only_one_wins(db, factory, clock):
    task, workers = _setup_task(db, factory, clock, num_workers=1)
    result = scheduling.schedule_task(db, clock, task.id)
    asm_id = result.assignment_id
    db.commit()

    barrier = Barrier(2)
    outcomes: list[str] = []

    def attempt():
        session = Session(get_engine())
        try:
            local_clock = FakeClock(start=clock.now())
            barrier.wait(timeout=10)
            scheduling.accept_invitation(session, local_clock, asm_id)
            session.commit()
            outcomes.append("accepted")
        except ConflictError:
            session.rollback()
            outcomes.append("conflict")
        except Exception as exc:  # pragma: no cover - surface unexpected errors
            session.rollback()
            outcomes.append(f"error:{type(exc).__name__}")
        finally:
            session.close()

    t1 = Thread(target=attempt)
    t2 = Thread(target=attempt)
    t1.start(); t2.start()
    t1.join(timeout=15); t2.join(timeout=15)

    assert sorted(outcomes).count("accepted") == 1, outcomes
    assert "conflict" in outcomes or outcomes.count("accepted") == 1

    with Session(get_engine()) as s:
        accepted = s.scalars(
            select(Assignment).where(
                Assignment.task_id == task.id,
                Assignment.status == AssignmentStatus.ACCEPTED.value,
            )
        ).all()
        assert len(accepted) == 1
        assert s.get(Task, task.id).status == TaskStatus.ASSIGNED.value


def test_accept_racing_with_expiry_sweep_single_survivor(db, factory, clock):
    task, workers = _setup_task(db, factory, clock, num_workers=2)
    result = scheduling.schedule_task(db, clock, task.id)
    asm_id = result.assignment_id
    db.commit()

    # Advance to exactly the deadline, then run accept and sweep concurrently.
    clock.advance(minutes=8)
    barrier = Barrier(2)
    outcomes: list[str] = []

    def accept_attempt():
        session = Session(get_engine())
        c = FakeClock(start=clock.now())
        try:
            barrier.wait(timeout=10)
            scheduling.accept_invitation(session, c, asm_id)
            session.commit()
            outcomes.append("accepted")
        except ConflictError:
            session.rollback()
            outcomes.append("conflict")
        finally:
            session.close()

    def sweep_attempt():
        session = Session(get_engine())
        c = FakeClock(start=clock.now())
        try:
            barrier.wait(timeout=10)
            scheduling.sweep_and_reallocate(session, c, task_ids=[task.id])
            session.commit()
            outcomes.append("swept")
        finally:
            session.close()

    t1 = Thread(target=accept_attempt)
    t2 = Thread(target=sweep_attempt)
    t1.start(); t2.start()
    t1.join(timeout=15); t2.join(timeout=15)

    assert len(outcomes) == 2
    with Session(get_engine()) as s:
        live = s.scalars(
            select(Assignment).where(
                Assignment.task_id == task.id,
                Assignment.status.in_(
                    [AssignmentStatus.PENDING.value, AssignmentStatus.ACCEPTED.value]
                ),
            )
        ).all()
        # Exactly one effective booking, regardless of commit order.
        assert len(live) == 1, [a.status.value for a in live]


def test_repeated_schedule_does_not_double_book(db, factory, clock):
    task, workers = _setup_task(db, factory, clock, num_workers=2)
    r1 = scheduling.schedule_task(db, clock, task.id)
    r2 = scheduling.schedule_task(db, clock, task.id)
    r3 = scheduling.schedule_task(db, clock, task.id)
    db.commit()
    assert r1.status == "invited"
    assert r2.status == "already_live"
    assert r3.assignment_id == r1.assignment_id
    pending = db.scalars(
        select(Assignment).where(
            Assignment.task_id == task.id,
            Assignment.status == AssignmentStatus.PENDING.value,
        )
    ).all()
    assert len(pending) == 1


def test_unsatisfiable_returns_specific_constraints(db, factory, clock):
    unit = factory.unit(tz="UTC")
    bls = factory.qualification(code="BLS")
    # One worker who lacks BLS.
    factory.worker(external_id="unqualified", units=[unit])
    plan = factory.plan(
        unit,
        timezone="UTC",
        period_days=1,
        slots=[
            {
                "weekday": d,
                "start_at": "09:00",
                "latest_start_at": "12:00",
                "duration_minutes": 60,
            }
            for d in range(7)
        ],
        qualification_ids=[bls.id],
    )
    tasks = generate_tasks(db, plan_id=plan.id, now=clock.now())
    db.commit()

    with Session(get_engine()) as s:
        result = scheduling.schedule_task(s, clock, tasks[0].id)
        s.rollback()
    assert result.status == "unsatisfiable"
    codes = {v["code"] for v in result.violations}
    assert "missing_qualification" in codes
