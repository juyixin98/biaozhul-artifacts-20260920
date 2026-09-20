"""Prerequisite gating and candidate ranking behaviour."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.enums import AssignmentStatus, TaskStatus
from app.models import Assignment
from app.services import scheduling
from app.services.generation import generate_tasks


def test_task_blocked_until_prerequisite_completed(db, factory, clock):
    unit = factory.unit(tz="UTC")
    bls = factory.qualification(code="BLS")
    worker = factory.worker(units=[unit])
    factory.grant(worker, bls)

    base = factory.plan(
        unit,
        title="base",
        timezone="UTC",
        period_days=1,
        qualification_ids=[bls.id],
        slots=[
            {
                "weekday": d,
                "start_at": "08:00",
                "latest_start_at": "09:00",
                "duration_minutes": 60,
            }
            for d in range(7)
        ],
    )
    dependent = factory.plan(
        unit,
        title="dependent",
        timezone="UTC",
        period_days=1,
        qualification_ids=[bls.id],
        prerequisite_plan_ids=[base.id],
        slots=[
            {
                "weekday": d,
                "start_at": "10:00",
                "latest_start_at": "11:00",
                "duration_minutes": 30,
            }
            for d in range(7)
        ],
    )
    # Regenerate both so prerequisite links resolve.
    base_tasks = generate_tasks(db, plan_id=base.id, now=clock.now())
    dep_tasks = generate_tasks(db, plan_id=dependent.id, now=clock.now())
    db.flush()

    dep_first = dep_tasks[0]
    assert dep_first.prerequisites, "prerequisite link should have been created"

    result = scheduling.schedule_task(db, clock, dep_first.id)
    assert result.status == "unsatisfiable"
    assert result.violations[0]["code"] == "prerequisite_not_completed"

    # Complete the prerequisite (assign + accept + complete), then retry.
    base_first = base_tasks[0]
    r = scheduling.schedule_task(db, clock, base_first.id)
    scheduling.accept_invitation(db, clock, r.assignment_id)
    base_first.status = TaskStatus.COMPLETED.value

    result2 = scheduling.schedule_task(db, clock, dep_first.id)
    assert result2.status == "invited"
