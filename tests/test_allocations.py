"""Allocation, accept/decline, 8-minute timeout and reallocation."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    Assignment,
    AssignmentEvent,
    AssignmentStatus,
    Task,
    TaskStatus,
)
from app.services import assignments as svc

UTC = timezone.utc


def _pending_task(db, make_unit, make_plan, make_task, **kw):
    unit = make_unit()
    plan = make_plan(unit, **kw.get("plan_kw", {}))
    task = make_task(
        plan,
        start=datetime(2026, 9, 22, 8, tzinfo=UTC),
        end=datetime(2026, 9, 22, 9, tzinfo=UTC),
        **kw.get("task_kw", {}),
    )
    return unit, plan, task


def test_allocate_creates_8min_invitation_for_best_worker(
    db, make_unit, make_plan, make_task, make_worker
):
    unit, plan, task = _pending_task(db, make_unit, make_plan, make_task)
    w1 = make_worker(unit, name="w1")
    w2 = make_worker(unit, name="w2")

    outcome = svc.allocate_task(db, task)
    assert outcome.allocated
    assert outcome.assignment.worker_id == w1.id  # id tiebreak
    assert outcome.assignment.status == AssignmentStatus.invited
    delta = outcome.assignment.expires_at - outcome.assignment.invited_at
    assert delta == timedelta(minutes=8)
    db.refresh(task)
    assert task.status == TaskStatus.invited


def test_allocate_is_idempotent_while_invitation_live(
    db, make_unit, make_plan, make_task, make_worker
):
    unit, plan, task = _pending_task(db, make_unit, make_plan, make_task)
    make_worker(unit)
    first = svc.allocate_task(db, task)
    second = svc.allocate_task(db, task)
    assert first.assignment.id == second.assignment.id
    assert db.scalar(
        select(func.count()).select_from(Assignment).where(Assignment.task_id == task.id)
    ) == 1


def test_allocate_returns_constraints_when_nobody_fits(
    db, make_unit, make_plan, make_task, make_worker
):
    unit = make_unit()
    plan = make_plan(unit, qualifications=["moving_handling"])
    task = make_task(
        plan,
        start=datetime(2026, 9, 22, 8, tzinfo=UTC),
        end=datetime(2026, 9, 22, 9, tzinfo=UTC),
    )
    make_worker(unit)  # active but unqualified
    outcome = svc.allocate_task(db, task)
    assert not outcome.allocated
    assert outcome.assignment is None
    codes = {v.code for v in outcome.violations}
    assert "qualification_missing_or_expired" in codes
    db.refresh(task)
    assert task.status == TaskStatus.pending  # nothing forced


def test_allocate_no_workers_reports_it(db, make_unit, make_plan, make_task):
    unit = make_unit()
    plan = make_plan(unit)
    task = make_task(
        plan,
        start=datetime(2026, 9, 22, 8, tzinfo=UTC),
        end=datetime(2026, 9, 22, 9, tzinfo=UTC),
    )
    outcome = svc.allocate_task(db, task)
    assert not outcome.allocated
    assert {v.code for v in outcome.violations} == {"no_workers"}


def test_accept_marks_assigned(db, make_unit, make_plan, make_task, make_worker):
    unit, plan, task = _pending_task(db, make_unit, make_plan, make_task)
    worker = make_worker(unit)
    offered = svc.allocate_task(db, task).assignment

    assignment, violation, replayed = svc.accept_invitation(db, task.id, worker.id)
    assert violation is None
    assert not replayed
    assert assignment.id == offered.id
    assert assignment.status == AssignmentStatus.assigned
    db.refresh(task)
    assert task.status == TaskStatus.assigned


def test_accept_by_other_worker_rejected(
    db, make_unit, make_plan, make_task, make_worker
):
    unit, plan, task = _pending_task(db, make_unit, make_plan, make_task)
    make_worker(unit, name="invited")
    other = make_worker(unit, name="other")
    svc.allocate_task(db, task)

    _, violation, _ = svc.accept_invitation(db, task.id, other.id)
    assert violation.code == "offer_belongs_to_other_worker"
    db.refresh(task)
    assert task.status == TaskStatus.invited


def test_invitation_lapses_after_8_minutes_and_reallocates(
    db, make_unit, make_plan, make_task, make_worker, advance
):
    unit, plan, task = _pending_task(db, make_unit, make_plan, make_task)
    w1 = make_worker(unit, name="w1")
    w2 = make_worker(unit, name="w2")
    offered = svc.allocate_task(db, task)
    assert offered.assignment.worker_id == w1.id

    advance(timedelta(minutes=8, seconds=1))
    outcomes = svc.reap_expired_invitations(db)
    assert len(outcomes) == 1
    assert outcomes[0].allocated
    # The worker who let it lapse is skipped on the immediate re-offer.
    assert outcomes[0].assignment.worker_id == w2.id
    assert outcomes[0].assignment.status == AssignmentStatus.invited

    rows = db.scalars(select(Assignment).where(Assignment.task_id == task.id)).all()
    by_status = {a.status: a.worker_id for a in rows}
    assert by_status[AssignmentStatus.expired] == w1.id
    assert by_status[AssignmentStatus.invited] == w2.id


def test_accept_after_expiry_fails_and_task_reoffers(
    db, make_unit, make_plan, make_task, make_worker, advance
):
    unit, plan, task = _pending_task(db, make_unit, make_plan, make_task)
    w1 = make_worker(unit, name="w1")
    make_worker(unit, name="w2")
    svc.allocate_task(db, task)

    advance(timedelta(minutes=8, seconds=1))
    _, violation, _ = svc.accept_invitation(db, task.id, w1.id)
    assert violation.code == "invitation_expired"

    # Task is pending again and can be offered out.
    db.refresh(task)
    assert task.status == TaskStatus.pending
    outcome = svc.allocate_task(db, task)
    assert outcome.allocated


def test_reaper_does_not_touch_unexpired_invitation(
    db, make_unit, make_plan, make_task, make_worker, advance
):
    unit, plan, task = _pending_task(db, make_unit, make_plan, make_task)
    w1 = make_worker(unit)
    first = svc.allocate_task(db, task)
    advance(timedelta(minutes=7, seconds=59))
    assert svc.reap_expired_invitations(db) == []
    db.refresh(first.assignment)
    assert first.assignment.status == AssignmentStatus.invited


def test_duplicate_idempotent_accept_replays_without_double_booking(
    db, make_unit, make_plan, make_task, make_worker
):
    unit, plan, task = _pending_task(db, make_unit, make_plan, make_task)
    worker = make_worker(unit)
    svc.allocate_task(db, task)

    a1, _, replayed1 = svc.accept_invitation(
        db, task.id, worker.id, request_key="key-1"
    )
    a2, _, replayed2 = svc.accept_invitation(
        db, task.id, worker.id, request_key="key-1"
    )
    assert replayed2 is True
    assert a1.id == a2.id
    count = db.scalar(
        select(func.count())
        .select_from(Assignment)
        .where(Assignment.task_id == task.id,
               Assignment.status == AssignmentStatus.assigned)
    )
    assert count == 1


def test_events_record_lifecycle(
    db, make_unit, make_plan, make_task, make_worker
):
    unit, plan, task = _pending_task(db, make_unit, make_plan, make_task)
    worker = make_worker(unit)
    svc.allocate_task(db, task)
    svc.accept_invitation(db, task.id, worker.id)

    actions = [
        e.action
        for e in db.scalars(
            select(AssignmentEvent).where(AssignmentEvent.task_id == task.id)
        ).all()
    ]
    assert "invitation_created" in actions
    assert "invitation_accepted" in actions


# --- real concurrency --------------------------------------------------------


def test_concurrent_accepts_only_one_wins():
    """Two threads accept the same invitation simultaneously; exactly one
    booking survives and the task never ends with two live assignments."""
    import threading
    from datetime import time

    from app.clock import clock
    from app.models import CarePlan, CareWorker, Recurrence, Unit

    setup = SessionLocal()
    now_unit = Unit(name="race-unit")
    setup.add(now_unit)
    setup.flush()
    w1 = CareWorker(name="race-w1", unit_id=now_unit.id, timezone="UTC",
                    active=True, created_at=clock.now())
    plan_obj = CarePlan(
        status="active", care_recipient_id="r", unit_id=now_unit.id,
        service_timezone="UTC", recurrence=Recurrence.daily,
        window_start=time(8), window_end=time(10), duration_minutes=60,
        required_qualifications=[], version=1,
        created_at=clock.now(), updated_at=clock.now(),
    )
    setup.add_all([w1, plan_obj])
    setup.flush()
    task_obj = Task(
        plan_id=plan_obj.id, plan_version=1, care_recipient_id="r",
        unit_id=now_unit.id, occurrence_key="2026-09-22",
        starts_at=datetime(2026, 9, 22, 8, tzinfo=UTC),
        ends_at=datetime(2026, 9, 22, 9, tzinfo=UTC),
        status=TaskStatus.invited, prerequisite_task_ids=[],
        created_at=clock.now(), updated_at=clock.now(),
    )
    setup.add(task_obj)
    setup.flush()
    offer = Assignment(
        task_id=task_obj.id, worker_id=w1.id, status=AssignmentStatus.invited,
        invited_at=clock.now(), expires_at=clock.now() + timedelta(minutes=8),
        created_via="auto", created_at=clock.now(),
    )
    setup.add(offer)
    setup.commit()
    task_id, worker_id = task_obj.id, w1.id
    setup.close()

    barrier = threading.Barrier(2)
    results: list = []

    def accept(request_key):
        session = SessionLocal()
        barrier.wait()
        try:
            results.append(
                svc.accept_invitation(
                    session, task_id, worker_id, request_key=request_key
                )
            )
        except Exception as exc:  # pragma: no cover - surfaced by asserts
            results.append(exc)
        finally:
            session.close()

    # Same worker, same idempotency key fired twice concurrently: a duplicate
    # request must never occupy hours twice.
    t1 = threading.Thread(target=accept, args=("dup-key",))
    t2 = threading.Thread(target=accept, args=("dup-key",))
    t1.start(); t2.start(); t1.join(); t2.join()

    check = SessionLocal()
    live = check.scalars(
        select(Assignment).where(
            Assignment.task_id == task_id,
            Assignment.status.in_((AssignmentStatus.invited,
                                  AssignmentStatus.assigned)),
        )
    ).all()
    task = check.get(Task, task_id)
    assert len(live) == 1
    assert live[0].status == AssignmentStatus.assigned
    assert task.status == TaskStatus.assigned
    assert all(not isinstance(r, BaseException) for r in results)
    assert {r[0].id for r in results} == {live[0].id}
    # Exactly one assigned row: the duplicate accept did not book again.
    assigned_count = check.scalar(
        select(func.count())
        .select_from(Assignment)
        .where(Assignment.task_id == task_id,
               Assignment.status == AssignmentStatus.assigned)
    )
    assert assigned_count == 1
    check.close()
