"""Manual coordinator adjustments and unit authorization."""

from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import (
    AssignmentEvent,
    AssignmentStatus,
    Task,
    TaskStatus,
)
from app.services import assignments as svc
from app.services import tasks as tasks_svc

UTC = timezone.utc


def _scenario(db, make_unit, make_coordinator, make_worker, make_plan,
              make_task, *, second_unit=False, qualified=True):
    unit = make_unit("North")
    other = make_unit("South") if second_unit else None
    coordinator = make_coordinator("Nora", units=[unit])
    outsider = make_coordinator("Sam", units=[other] if second_unit else [])
    plan = make_plan(unit, qualifications=["personal_care"])
    task = make_task(
        plan,
        start=datetime(2026, 9, 22, 8, tzinfo=UTC),
        end=datetime(2026, 9, 22, 9, tzinfo=UTC),
    )
    if qualified:
        worker = make_worker(
            unit,
            qualifications=[(
                "personal_care",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2027, 1, 1, tzinfo=UTC),
            )],
        )
    else:
        # No qualification rows -> cannot satisfy the plan's requirement.
        worker = make_worker(unit)
    return unit, other, coordinator, outsider, plan, task, worker


def test_coordinator_without_unit_cannot_allocate(
    db, make_unit, make_coordinator, make_worker, make_plan, make_task
):
    _, _, _, outsider, _, task, _ = _scenario(
        db, make_unit, make_coordinator, make_worker, make_plan, make_task,
        second_unit=True,
    )
    with pytest.raises(svc.AuthorizationError):
        svc.allocate_task(db, task, coordinator_id=outsider.id)


def test_manual_assign_runs_constraint_checks_and_records_reason(
    db, make_unit, make_coordinator, make_worker, make_plan, make_task
):
    _, _, coordinator, _, plan, task, worker = _scenario(
        db, make_unit, make_coordinator, make_worker, make_plan, make_task,
        qualified=False,
    )
    # Worker lacks the required qualification: the booking is refused.
    outcome = svc.manual_assign(
        db, task.id, worker.id, reason="family preference",
        coordinator_id=coordinator.id,
    )
    assert not outcome.allocated
    assert any(v.code == "qualification_missing_or_expired"
               for v in outcome.violations)
    db.refresh(task)
    assert task.status == TaskStatus.pending

    events = db.scalars(select(AssignmentEvent)).all()
    assert any(e.action == "manual_assign_rejected"
               and e.reason == "family preference" for e in events)


def test_manual_assign_succeeds_with_reason_history(
    db, make_unit, make_coordinator, make_worker, make_plan, make_task
):
    _, _, coordinator, _, plan, task, worker = _scenario(
        db, make_unit, make_coordinator, make_worker, make_plan, make_task,
    )
    outcome = svc.manual_assign(
        db, task.id, worker.id, reason="client requested same carer",
        coordinator_id=coordinator.id,
    )
    assert outcome.allocated
    assert outcome.assignment.created_via == "manual"
    assert outcome.assignment.created_by_coordinator_id == coordinator.id

    event = db.scalars(
        select(AssignmentEvent).where(AssignmentEvent.action == "manual_assign")
    ).one()
    assert event.reason == "client requested same carer"
    assert event.worker_id == worker.id


def test_manual_assign_cannot_override_assigned_task(
    db, make_unit, make_coordinator, make_worker, make_plan, make_task
):
    from app.clock import clock
    from app.models import CareWorker

    _, _, coordinator, _, plan, task, worker = _scenario(
        db, make_unit, make_coordinator, make_worker, make_plan, make_task,
    )
    svc.manual_assign(db, task.id, worker.id, reason="first",
                      coordinator_id=coordinator.id)
    second = CareWorker(
        name="second", unit_id=task.unit_id, timezone="UTC", active=True,
        created_at=clock.now(),
    )
    db.add(second)
    db.flush()
    with pytest.raises(svc.ManualError):
        svc.manual_assign(db, task.id, second.id, reason="swap",
                          coordinator_id=coordinator.id)


def test_reschedule_checks_constraints_and_reallocates(
    db, make_unit, make_coordinator, make_worker, make_plan, make_task
):
    _, _, coordinator, _, plan, task, worker = _scenario(
        db, make_unit, make_coordinator, make_worker, make_plan, make_task,
    )
    outcome = svc.manual_assign(
        db, task.id, worker.id, reason="booked", coordinator_id=coordinator.id
    )
    assert outcome.allocated

    # Move within the same service date; the same worker is excluded on the
    # automatic re-offer (their cancelled booking still ends at the old time,
    # but the exclusion rule prevents immediate re-offer ping-pong).
    outcome = svc.reschedule_task(
        db, task.id,
        datetime(2026, 9, 22, 9, 30, tzinfo=UTC),
        datetime(2026, 9, 22, 10, 15, tzinfo=UTC),
        reason="client appointment moved",
        coordinator_id=coordinator.id,
    )
    db.refresh(task)
    assert task.starts_at.hour == 9
    assert task.starts_at.minute == 30

    event = db.scalars(
        select(AssignmentEvent).where(AssignmentEvent.action == "manual_reschedule")
    ).one()
    assert event.reason == "client appointment moved"


def test_reschedule_cannot_cross_service_date(
    db, make_unit, make_coordinator, make_worker, make_plan, make_task
):
    _, _, coordinator, _, plan, task, worker = _scenario(
        db, make_unit, make_coordinator, make_worker, make_plan, make_task,
    )
    with pytest.raises(svc.ManualError):
        svc.reschedule_task(
            db, task.id,
            datetime(2026, 9, 23, 8, tzinfo=UTC),
            datetime(2026, 9, 23, 9, tzinfo=UTC),
            reason="next day", coordinator_id=coordinator.id,
        )


def test_cancel_releases_task(db, make_unit, make_coordinator, make_worker,
                              make_plan, make_task):
    _, _, coordinator, _, plan, task, worker = _scenario(
        db, make_unit, make_coordinator, make_worker, make_plan, make_task,
    )
    svc.manual_assign(db, task.id, worker.id, reason="booked",
                      coordinator_id=coordinator.id)
    cancelled = svc.cancel_task(db, task.id, reason="client hospitalised",
                                coordinator_id=coordinator.id)
    assert cancelled.status == TaskStatus.cancelled
    from app.models import Assignment
    assignment = db.scalars(
        select(Assignment).where(Assignment.task_id == task.id)
    ).one()
    assert assignment.status == AssignmentStatus.cancelled


def test_prerequisite_must_complete_first(
    db, make_unit, make_coordinator, make_worker, make_plan, make_task
):
    unit = make_unit()
    coordinator = make_coordinator("Nora", units=[unit])
    bath_plan = make_plan(unit, window_start=time(8), window_end=time(9))
    dress_plan = make_plan(unit, window_start=time(10), window_end=time(11))
    bath = make_task(
        bath_plan,
        start=datetime(2026, 9, 22, 8, tzinfo=UTC),
        end=datetime(2026, 9, 22, 8, 30, tzinfo=UTC),
        occurrence="2026-09-22",
    )
    dress = make_task(
        dress_plan,
        start=datetime(2026, 9, 22, 10, tzinfo=UTC),
        end=datetime(2026, 9, 22, 10, 45, tzinfo=UTC),
        occurrence="2026-09-22",
        prerequisite_task_ids=[bath.id],
    )
    worker = make_worker(unit)
    second_worker = make_worker(unit, name="other-carer")
    svc.manual_assign(db, bath.id, worker.id, reason="x",
                      coordinator_id=coordinator.id)
    svc.manual_assign(db, dress.id, second_worker.id, reason="x",
                      coordinator_id=coordinator.id)

    with pytest.raises(tasks_svc.TaskStateError):
        tasks_svc.complete_task(db, dress.id, coordinator_id=coordinator.id)

    tasks_svc.complete_task(db, bath.id, coordinator_id=coordinator.id)
    done = tasks_svc.complete_task(db, dress.id, coordinator_id=coordinator.id)
    assert done.status == TaskStatus.completed
