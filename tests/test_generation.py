"""Task generation: 14-day horizon, idempotency, timezone anchoring, revisions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Assignment, CarePlan, Invitation, Task, TaskStatus
from app.services import generation as gen
from tests.conftest import CNA, grant_qualification, make_qualification, make_unit, make_worker


def _plan_payload(unit_id: int, *, templates=None, tz="Asia/Shanghai"):
    return {
        "external_id": "P-1",
        "client_name": "Client",
        "unit_id": unit_id,
        "timezone": tz,
        "templates": templates if templates is not None else [
            {
                "code": "DAILY",
                "name": "Daily visit",
                "window_start_minute": 9 * 60,
                "window_end_minute": 10 * 60,
                "duration_minutes": 45,
                "weekday_mask": [],
                "qualification_codes": [],
                "prerequisite_codes": [],
            }
        ],
    }


def test_generates_14_service_days_idempotently(db: Session, frozen_now):
    unit = make_unit(db)
    plan = gen.create_plan(db, **_plan_payload(unit.id), now=frozen_now)
    first_count = db.scalar(select(func.count()).select_from(Task).where(Task.plan_id == plan.id))
    assert first_count == 14  # frozen at 08:00 local: today's 09:00 visit still ahead

    # Repeated generation must not duplicate.
    again = gen.generate_tasks(db, plan, now=frozen_now)
    assert again == []
    total = db.scalar(select(func.count()).select_from(Task).where(Task.plan_id == plan.id))
    assert total == 14


def test_skips_occurrences_already_past(db: Session):
    # 16:00 UTC = 00:00 Shanghai next day; a 09:00 local template on "today" is past.
    now = datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc)
    unit = make_unit(db)
    plan = gen.create_plan(db, **_plan_payload(unit.id), now=now)
    dates = sorted(t.scheduled_date for t in db.scalars(select(Task).where(Task.plan_id == plan.id)))
    assert dates[0] == "2026-09-22"
    assert len(dates) == 14


def test_weekday_mask(db: Session, frozen_now):
    unit = make_unit(db)
    payload = _plan_payload(unit.id, templates=[{
        "code": "MWF",
        "name": "Mon/Wed/Fri",
        "window_start_minute": 10 * 60,
        "window_end_minute": 12 * 60,
        "duration_minutes": 30,
        "weekday_mask": [1, 3, 5],
        "qualification_codes": [],
        "prerequisite_codes": [],
    }])
    plan = gen.create_plan(db, **payload, now=frozen_now)
    dates = [t.scheduled_date for t in db.scalars(select(Task).where(Task.plan_id == plan.id))]
    # 2026-09-21 (Mon) .. 2026-10-04 (Sun): Mon/Wed/Fri occurrences
    assert dates == ["2026-09-21", "2026-09-23", "2026-09-25",
                     "2026-09-28", "2026-09-30", "2026-10-02"]


def test_timezone_anchoring(db: Session, frozen_now):
    unit = make_unit(db)
    plan = gen.create_plan(db, **_plan_payload(unit.id), now=frozen_now)
    task = db.scalar(select(Task).where(Task.plan_id == plan.id, Task.scheduled_date == "2026-09-21"))
    # 09:00 Shanghai == 01:00 UTC
    assert task.scheduled_start == datetime(2026, 9, 21, 1, 0, tzinfo=timezone.utc)
    assert task.scheduled_end == datetime(2026, 9, 21, 1, 45, tzinfo=timezone.utc)


def test_prerequisites_linked_on_same_date(db: Session, frozen_now):
    unit = make_unit(db)
    templates = [
        {"code": "A", "name": "A", "window_start_minute": 8 * 60, "window_end_minute": 9 * 60,
         "duration_minutes": 30, "weekday_mask": [], "qualification_codes": [], "prerequisite_codes": []},
        {"code": "B", "name": "B", "window_start_minute": 10 * 60, "window_end_minute": 11 * 60,
         "duration_minutes": 30, "weekday_mask": [], "qualification_codes": [], "prerequisite_codes": ["A"]},
    ]
    plan = gen.create_plan(db, **_plan_payload(unit.id, templates=templates), now=frozen_now)
    rows = db.scalars(
        select(Task).where(Task.plan_id == plan.id, Task.scheduled_date == "2026-09-21")
        .order_by(Task.scheduled_start)
    ).all()
    a_task, b_task = rows[0], rows[1]
    assert b_task.prerequisites == [{"task_id": a_task.id, "template_code": "A"}]


def test_revision_cancels_future_but_keeps_started(db: Session, frozen_now):
    unit = make_unit(db)
    plan = gen.create_plan(db, **_plan_payload(unit.id), now=frozen_now)
    tasks = list(db.scalars(select(Task).where(Task.plan_id == plan.id).order_by(Task.scheduled_start)))

    # Simulate: first task was already assigned; its start (01:00 UTC) is in
    # the future vs frozen_now, so start it first at a later clock.
    worker = make_worker(db, unit)
    from app.models import Assignment, AssignmentStatus, Invitation, InvitationStatus
    inv = Invitation(task_id=tasks[0].id, worker_id=worker.id, round=1,
                     status=InvitationStatus.ACCEPTED.value, created_at=frozen_now,
                     expires_at=frozen_now, responded_at=frozen_now)
    db.add(inv)
    db.flush()
    db.add(Assignment(task_id=tasks[0].id, worker_id=worker.id, invitation_id=inv.id,
                      status=AssignmentStatus.ASSIGNED.value, assigned_at=frozen_now))
    tasks[0].status = TaskStatus.IN_PROGRESS.value
    db.flush()

    # Revise after the first occurrence has started.
    later = tasks[0].scheduled_start + timedelta(minutes=10)
    new_plan = gen.revise_plan(
        db, external_id="P-1", client_name="Client", timezone="Asia/Shanghai",
        templates=[{
            "code": "DAILY", "name": "Daily visit v2",
            "window_start_minute": 11 * 60, "window_end_minute": 12 * 60,
            "duration_minutes": 20, "weekday_mask": [],
            "qualification_codes": [], "prerequisite_codes": [],
        }],
        now=later,
    )
    db.refresh(plan)
    assert plan.active is False
    assert new_plan.revision == 2 and new_plan.active is True

    # Started task survives on the old revision, still assigned.
    db.refresh(tasks[0])
    assert tasks[0].status == TaskStatus.IN_PROGRESS.value
    old_assignment = db.scalar(select(Assignment).where(Assignment.task_id == tasks[0].id))
    assert old_assignment is not None

    # Other future tasks are cancelled and their assignments/invitations released.
    future_old = [t for t in tasks[1:]]
    assert all(t.status == TaskStatus.CANCELLED.value for t in future_old)

    # New revision generates fresh tasks at the new time.
    new_tasks = list(db.scalars(select(Task).where(Task.plan_id == new_plan.id)))
    assert len(new_tasks) == 14  # 09/21 11:00 local still in the future -> 14 days
    assert all(str(t.scheduled_start.astimezone(timezone.utc).hour) == "3" for t in new_tasks)
    assert all(t.status == TaskStatus.PLANNED.value for t in new_tasks)


def test_create_plan_rejects_duplicate_external_id(db: Session, frozen_now):
    unit = make_unit(db)
    gen.create_plan(db, **_plan_payload(unit.id), now=frozen_now)
    with pytest.raises(gen.GenerationError):
        gen.create_plan(db, **_plan_payload(unit.id), now=frozen_now)
