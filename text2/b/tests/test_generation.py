"""Task generation: 14-day horizon, idempotency, revision isolation."""
from __future__ import annotations

from datetime import time

from sqlalchemy import func, select

from app.enums import TaskStatus
from app.models import Task
from app.services.generation import generate_tasks
from app.services.plans import revise_plan


def test_generates_14_days_no_more_no_less(db, factory, clock):
    unit = factory.unit(tz="UTC")
    # Daily plan, every weekday, one slot.
    plan = factory.plan(
        unit,
        timezone="UTC",
        period_days=1,
        slots=[
            {
                "weekday": d,
                "start_at": "09:00",
                "latest_start_at": "09:30",
                "duration_minutes": 30,
            }
            for d in range(7)
        ],
    )
    # factory.plan already triggers one generation.
    tasks = generate_tasks(db, plan_id=plan.id, now=clock.now())
    assert len(tasks) == 14
    assert all(t.status == TaskStatus.PENDING.value for t in tasks)


def test_repeated_generation_creates_no_duplicates(db, factory, clock):
    unit = factory.unit(tz="UTC")
    plan = factory.plan(unit, period_days=1)

    first = generate_tasks(db, plan_id=plan.id, now=clock.now())
    count_first = db.scalar(select(func.count(Task.id)).where(Task.plan_id == plan.id))
    second = generate_tasks(db, plan_id=plan.id, now=clock.now())
    third = generate_tasks(db, plan_id=plan.id, now=clock.now())
    count_after = db.scalar(select(func.count(Task.id)).where(Task.plan_id == plan.id))

    assert count_first == count_after
    assert len(second) == len(first)
    assert len(third) == len(first)
    # Unique occurrence dates.
    dates = [t.occurrence_date for t in first]
    assert len(set(dates)) == len(dates)


def test_periodicity_7_days_fires_only_on_anchor_weekday(db, factory, clock):
    unit = factory.unit(tz="UTC")
    plan = factory.plan(unit, period_days=7)  # default Monday slot
    tasks = generate_tasks(db, plan_id=plan.id, now=clock.now())
    # 14-day horizon from Monday -> exactly two Mondays.
    assert len(tasks) == 2
    assert {t.occurrence_date.weekday() for t in tasks} == {0}


def test_revision_only_replaces_unstarted_tasks(db, factory, clock):
    from app.services import scheduling

    unit = factory.unit(tz="UTC")
    bls = factory.qualification(code="BLS")
    worker = factory.worker(units=[unit], tz="UTC")
    factory.grant(worker, bls)
    plan = factory.plan(
        unit,
        timezone="UTC",
        period_days=1,
        qualification_ids=[bls.id],
        slots=[
            {
                "weekday": d,
                "start_at": "09:00",
                "latest_start_at": "10:00",
                "duration_minutes": 60,
            }
            for d in range(7)
        ],
    )
    tasks = generate_tasks(db, plan_id=plan.id, now=clock.now())
    first_task = tasks[0]

    # Accept the first task -> it is "started"/committed.
    result = scheduling.schedule_task(db, clock, first_task.id)
    assert result.status == "invited"
    scheduling.accept_invitation(db, clock, result.assignment_id)
    db.commit()

    # Revise: new window 14:00 for every day.
    coordinator = factory.coordinator(external_id="coord-revise", units=[unit])
    revise_plan(
        db,
        clock,
        plan_id=plan.id,
        slots=[
            {
                "weekday": d,
                "start_at": "14:00",
                "latest_start_at": "15:00",
                "duration_minutes": 45,
            }
            for d in range(7)
        ],
        change_note="move to afternoon",
        coordinator_id=coordinator.id,
    )
    db.commit()

    db.expire_all()
    protected = db.get(Task, first_task.id)
    # Accepted task is untouched: still on old version with its 09:00 slot.
    assert protected.status == TaskStatus.ASSIGNED.value
    assert protected.duration_minutes == 60
    assert protected.plan_version_id == 1

    new_tasks = db.scalars(
        select(Task).where(Task.plan_version_id == 2)
    ).all()
    assert len(new_tasks) == 14
    # UTC timezone: 14:00 local stays 14:00 UTC.
    assert all(t.duration_minutes == 45 for t in new_tasks)
    assert all(t.starts_at.hour == 14 for t in new_tasks)

    old_pending = db.scalars(
        select(Task).where(
            Task.plan_version_id == 1,
            Task.id != first_task.id,
        )
    ).all()
    assert all(t.status == TaskStatus.CANCELLED.value for t in old_pending)
