"""Coordinator unit authorization and manual adjustments with audit trail."""
from __future__ import annotations

import pytest

from app.enums import AssignmentStatus, EventType, TaskStatus
from app.models import Assignment, AssignmentEvent
from app.services import scheduling
from app.services.errors import AuthorizationError
from app.services.generation import generate_tasks


def _world(db, factory, clock):
    unit = factory.unit(name="u1", tz="UTC")
    other = factory.unit(name="u2", tz="UTC")
    coord = factory.coordinator(external_id="allowed", units=[unit])
    outsider = factory.coordinator(external_id="denied", units=[other])
    bls = factory.qualification(code="BLS")
    worker = factory.worker(units=[unit])
    factory.grant(worker, bls)
    plan = factory.plan(unit, qualification_ids=[bls.id], coordinator=coord)
    tasks = generate_tasks(db, plan_id=plan.id, now=clock.now())
    return unit, other, coord, outsider, worker, tasks[0]


def test_coordinator_can_only_schedule_authorised_unit(db, factory, clock):
    unit, other, coord, outsider, worker, task = _world(db, factory, clock)

    # Allowed: no error.
    coord.ensure_unit(unit.id)

    with pytest.raises(AuthorizationError):
        outsider.ensure_unit(unit.id)
    with pytest.raises(AuthorizationError):
        outsider.ensure_can_access_task(db, task.id)


def test_manual_assign_runs_same_constraints_and_logs(db, factory, clock):
    unit, _, coord, _, worker, task = _world(db, factory, clock)
    unqualified = factory.worker(external_id="no-bls", units=[unit])

    bad = scheduling.manual_assign(
        db,
        clock,
        task_id=task.id,
        worker_id=unqualified.id,
        coordinator_id=coord.id,
        reason="try unqualified",
    )
    assert bad.status == "unsatisfiable"
    assert any(v["code"] == "missing_qualification" for v in bad.violations)

    good = scheduling.manual_assign(
        db,
        clock,
        task_id=task.id,
        worker_id=worker.id,
        coordinator_id=coord.id,
        reason="family request",
    )
    assert good.status == "invited"
    db.flush()

    events = [
        e
        for e in db.query(AssignmentEvent).all()
        if e.event_type == EventType.MANUAL_ASSIGN
    ]
    assert events, "manual assign must be audited"
    last = events[-1]
    assert last.coordinator_id == coord.id
    assert '"reason": "family request"' in (last.detail or "") or "family request" in (last.detail or "")


def test_manual_reschedule_evicts_incompatible_assignee(db, factory, clock):
    from datetime import datetime, timedelta, timezone

    unit, _, coord, _, worker, task = _world(db, factory, clock)
    r = scheduling.schedule_task(db, clock, task.id)
    scheduling.accept_invitation(db, clock, r.assignment_id)
    db.flush()

    # Move to a slot exactly 3 hours later: violates the 10h rest relative to
    # the *same* accepted assignment? The existing assignment is excluded from
    # self-comparison, so instead move onto a time that overlaps a second
    # accepted booking we create on another task.
    tasks = generate_tasks(db, plan_id=task.plan_id, now=clock.now())
    second = next(t for t in tasks if t.id != task.id)
    # Put an accepted assignment for the worker on "second" automatically.
    r2 = scheduling.schedule_task(db, clock, second.id)
    scheduling.accept_invitation(db, clock, r2.assignment_id)
    db.flush()

    # Move first task onto the second task's interval -> overlap -> assignee
    # must be evicted and task returned to pending.
    moved = scheduling.manual_reschedule(
        db,
        clock,
        task_id=task.id,
        starts_at=second.starts_at,
        duration_minutes=second.duration_minutes,
        coordinator_id=coord.id,
        reason="client hospital visit",
    )
    db.flush()
    assert moved.status == TaskStatus.PENDING.value
    assert bool(moved.manually_adjusted)

    evicted = db.get(Assignment, r.assignment_id)
    assert evicted.status == AssignmentStatus.CANCELLED.value

    logged = db.query(AssignmentEvent).filter(
        AssignmentEvent.event_type == EventType.MANUAL_RESCHEDULE
    ).count()
    assert logged >= 1


def test_manual_unassign_releases_task(db, factory, clock):
    unit, _, coord, _, worker, task = _world(db, factory, clock)
    r = scheduling.schedule_task(db, clock, task.id)
    scheduling.accept_invitation(db, clock, r.assignment_id)
    db.flush()

    scheduling.manual_unassign(
        db, clock, task_id=task.id, coordinator_id=coord.id, reason="worker sick"
    )
    db.flush()
    assert task.status == TaskStatus.PENDING.value
    assert db.get(Assignment, r.assignment_id).status == AssignmentStatus.CANCELLED.value
