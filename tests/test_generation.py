"""Task generation, idempotency and plan revision rules."""

from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select

from app.models import PlanStatus, Recurrence, Task, TaskStatus
from app.schemas import PlanUpdate
from app.services import plans as plans_service

UTC = timezone.utc


def test_generates_14_daily_tasks(db, make_unit, make_plan):
    unit = make_unit()
    plan = make_plan(unit, tz="UTC")
    stats = plans_service.generate_tasks(db, plan)
    assert stats.created == 14
    assert len(stats.tasks) == 14
    # First visit starts at the window's beginning on the frozen day.
    first = min(stats.tasks, key=lambda t: t.starts_at)
    assert first.starts_at == datetime(2026, 9, 21, 8, 0, tzinfo=UTC)
    assert first.ends_at == datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


def test_repeated_generation_does_not_duplicate(db, make_unit, make_plan):
    unit = make_unit()
    plan = make_plan(unit)
    first = plans_service.generate_tasks(db, plan)
    db.expire_all()
    second = plans_service.generate_tasks(db, plan)
    assert first.created == 14
    assert second.created == 0
    assert second.updated == 0
    assert len(second.tasks) == 14


def test_weekly_plan_only_generates_matching_weekday(db, make_unit, make_plan):
    unit = make_unit()
    # Frozen day Monday 2026-09-21; ask for Wednesdays (weekday 2).
    plan = make_plan(unit, recurrence=Recurrence.weekly, day_of_week=2)
    stats = plans_service.generate_tasks(db, plan)
    # Wednesdays within 2026-09-21 .. 2026-10-04: 09-23, 09-30.
    assert stats.created == 2
    assert {t.occurrence_key for t in stats.tasks} == {"2026-09-23", "2026-09-30"}


def test_revision_updates_pending_but_locks_assigned(db, make_unit, make_plan,
                                                     make_task, make_worker,
                                                     make_assignment):
    unit = make_unit()
    plan = make_plan(unit, window_start=time(8), window_end=time(10), duration=60)
    stats = plans_service.generate_tasks(db, plan)
    day1, day2 = sorted(stats.tasks, key=lambda t: t.starts_at)[:2]

    # Simulate day1 already accepted/assigned; it must be untouched by revision.
    worker = make_worker(unit)
    day1.status = TaskStatus.assigned
    day1.plan_version = plan.version
    db.commit()

    plan.window_start = time(12)
    plan.window_end = time(14)
    db.commit()
    plan, stats2 = plans_service.update_plan(
        db, plan.id,
        PlanUpdate(window_start=time(12), window_end=time(14)),
    )

    db.refresh(day1)
    db.refresh(day2)
    assert plan.version == 2
    # Locked task keeps old timing and old version.
    assert day1.plan_version == 1
    assert day1.starts_at.hour == 8
    # Not-yet-started task moves and takes the new version.
    assert day2.plan_version == 2
    assert day2.starts_at.hour == 12


def test_revision_lapses_outstanding_invitation(db, make_unit, make_plan,
                                                 make_worker, make_assignment):
    from app.models import AssignmentStatus

    unit = make_unit()
    plan = make_plan(unit)
    stats = plans_service.generate_tasks(db, plan)
    day1 = sorted(stats.tasks, key=lambda t: t.starts_at)[0]
    worker = make_worker(unit)
    make_assignment(day1, worker, status=AssignmentStatus.invited)
    day1.status = TaskStatus.invited
    db.commit()

    plan, _ = plans_service.update_plan(db, plan.id, PlanUpdate(duration_minutes=90))

    db.refresh(day1)
    assert day1.status == TaskStatus.pending
    expired = [
        a for a in day1.assignments if a.status == AssignmentStatus.expired
    ]
    assert len(expired) == 1
    assert day1.ends_at - day1.starts_at == timedelta(minutes=90)


def test_changing_daily_to_weekly_cancels_stale_unstarted(db, make_unit, make_plan):
    from app.models import AssignmentStatus
    from app.schemas import PlanUpdate

    unit = make_unit()
    plan = make_plan(unit)
    stats = plans_service.generate_tasks(db, plan)
    assert len(stats.tasks) == 14

    plan, stats2 = plans_service.update_plan(
        db, plan.id, PlanUpdate(recurrence=Recurrence.weekly, day_of_week=0)
    )
    # Only Mondays remain; the other pending visits are cancelled, none locked.
    surviving = [
        t for t in stats2.tasks if t.status != TaskStatus.cancelled
    ]
    assert all(t.occurrence_key.endswith("-21") or t.occurrence_key.endswith("-28")
               for t in surviving)
    assert stats2.cancelled_stale == 12


def test_cancelled_plan_creates_no_more_tasks(db, make_unit, make_plan):
    unit = make_unit()
    plan = make_plan(unit)
    plans_service.generate_tasks(db, plan)
    plan.status = PlanStatus.cancelled
    db.commit()
    # Another occurrence enters the horizon as days pass; it must not generate.
    from app.clock import clock
    clock.set(clock.now() + timedelta(days=1))
    stats = plans_service.generate_tasks(db, plan)
    assert stats.created == 0


def test_date_re_entering_schedule_revives_cancelled_task(db, make_unit,
                                                          make_plan):
    """daily -> weekly cancels weekdays; switching back to daily revives the
    same rows instead of colliding with the unique (plan_id, date) key."""
    unit = make_unit()
    plan = make_plan(unit)
    first = plans_service.generate_tasks(db, plan)
    assert first.created == 14

    plan, stats = plans_service.update_plan(
        db, plan.id, PlanUpdate(recurrence=Recurrence.weekly, day_of_week=0)
    )
    tuesday = db.scalars(
        select(Task).where(Task.plan_id == plan.id,
                           Task.occurrence_key == "2026-09-22")
    ).one()
    assert tuesday.status == TaskStatus.cancelled

    plan, stats2 = plans_service.update_plan(
        db, plan.id, PlanUpdate(recurrence=Recurrence.daily)
    )
    db.expire_all()
    revived = db.scalars(
        select(Task).where(Task.plan_id == plan.id,
                           Task.occurrence_key == "2026-09-22")
    ).one()
    assert revived.id == tuesday.id
    assert revived.status == TaskStatus.pending
    assert revived.plan_version == plan.version
    # No new row for that date: revived, not duplicated.
    assert stats2.created == 0
    assert stats2.updated >= 1


def test_prerequisites_link_same_day_tasks(db, make_unit, make_plan):
    from app.models import plan_prerequisites
    from sqlalchemy import select

    unit = make_unit()
    bath = make_plan(unit, window_start=time(8), window_end=time(9), duration=30)
    dress = make_plan(unit, window_start=time(10), window_end=time(11), duration=30)
    plans_service.generate_tasks(db, bath)
    db.execute(
        plan_prerequisites.insert().values(
            plan_id=dress.id, prerequisite_plan_id=bath.id
        )
    )
    db.commit()
    stats = plans_service.generate_tasks(db, dress)
    bath_today = db.scalars(
        select(Task).where(Task.plan_id == bath.id)
    ).all()
    bath_by_day = {t.occurrence_key: t.id for t in bath_today}
    for task in stats.tasks:
        assert task.prerequisite_task_ids == [bath_by_day[task.occurrence_key]]


def test_service_timezone_schedules_in_local_time(db, make_unit, make_plan):
    unit = make_unit()
    # Helsinki is UTC+3 in late September (EEST).
    plan = make_plan(unit, tz="Europe/Helsinki",
                     window_start=time(8), window_end=time(10))
    stats = plans_service.generate_tasks(db, plan)
    first = min(stats.tasks, key=lambda t: t.starts_at)
    assert first.starts_at == datetime(2026, 9, 21, 5, 0, tzinfo=UTC)
