"""Care-plan management and 14-day task generation.

Generation is idempotent: the (plan_id, service-local date) pair is unique, so
running generation twice never creates duplicate tasks. A plan revision only
touches not-yet-started work — ``pending`` tasks are updated and outstanding
``invited`` offers are lapsed and recreated, while ``assigned`` and
``completed`` tasks stay locked on their old version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import clock
from app.config import get_settings
from app.models import (
    Assignment,
    AssignmentEvent,
    AssignmentStatus,
    CarePlan,
    PlanStatus,
    Recurrence,
    Task,
    TaskStatus,
    plan_prerequisites,
)
from app.schemas import PlanIn, PlanUpdate
from app.timeutils import get_zone, local_to_utc, utc_in_zone


class PlanValidationError(ValueError):
    pass


class NotFoundError(KeyError):
    pass


# --- helpers ----------------------------------------------------------------


def prerequisite_plan_ids(db: Session, plan_id: int) -> list[int]:
    rows = db.execute(
        select(plan_prerequisites.c.prerequisite_plan_id).where(
            plan_prerequisites.c.plan_id == plan_id
        )
    ).scalars().all()
    return list(rows)


def _window_for(plan: CarePlan, local_date) -> tuple[datetime, datetime]:
    zone = get_zone(plan.service_timezone)
    starts_at = local_to_utc(local_date, plan.window_start, zone)
    ends_at = local_to_utc(local_date, plan.window_end, zone)
    # Scheduled visit occupies the first ``duration_minutes`` of the window;
    # the remainder is coordinator slack for manual moves.
    scheduled_end = starts_at + timedelta(minutes=plan.duration_minutes)
    return starts_at, min(scheduled_end, ends_at)


def _occurrence_dates(plan: CarePlan, horizon_days: int):
    zone = get_zone(plan.service_timezone)
    today = utc_in_zone(clock.now(), zone).date()
    for offset in range(horizon_days):
        day = today + timedelta(days=offset)
        if plan.recurrence == Recurrence.daily or day.weekday() == plan.day_of_week:
            yield day


def _lapse_offer(db: Session, task: Task, assignment: Assignment, *, actor: str,
                 reason: str) -> None:
    assignment.status = AssignmentStatus.expired
    assignment.responded_at = clock.now()
    task.status = TaskStatus.pending
    db.add(
        AssignmentEvent(
            assignment_id=assignment.id,
            task_id=task.id,
            worker_id=assignment.worker_id,
            action="invitation_lapsed",
            reason=reason,
            actor=actor,
            detail={"plan_revision": True},
            created_at=clock.now(),
        )
    )


# --- plan CRUD --------------------------------------------------------------


def _validate_windows(recurrence, day_of_week, window_start, window_end,
                      duration_minutes: int) -> None:
    if recurrence == Recurrence.weekly and day_of_week is None:
        raise PlanValidationError("day_of_week is required for weekly plans")
    if duration_minutes <= 0:
        raise PlanValidationError("duration_minutes must be positive")
    if window_end <= window_start:
        raise PlanValidationError("window_end must be later than window_start")
    window_minutes = (
        datetime.combine(datetime.min, window_end)
        - datetime.combine(datetime.min, window_start)
    ).total_seconds() / 60
    if duration_minutes > window_minutes:
        raise PlanValidationError(
            f"duration {duration_minutes}m does not fit the {int(window_minutes)}m window"
        )


def _validate_prerequisites(db: Session, ids: list[int], recipient_id: str) -> None:
    if not ids:
        return
    plans = db.scalars(select(CarePlan).where(CarePlan.id.in_(ids))).all()
    if len(plans) != len(set(ids)):
        raise PlanValidationError("Unknown or duplicated prerequisite plan id")
    for p in plans:
        if p.care_recipient_id != recipient_id:
            raise PlanValidationError(
                f"Prerequisite plan {p.id} serves a different care recipient"
            )


def create_plan(db: Session, payload: PlanIn, *, actor: str = "coordinator") -> CarePlan:
    _validate_windows(
        payload.recurrence, payload.day_of_week, payload.window_start,
        payload.window_end, payload.duration_minutes,
    )
    get_zone(payload.service_timezone)
    _validate_prerequisites(db, payload.prerequisite_plan_ids, payload.care_recipient_id)

    now = clock.now()
    plan = CarePlan(
        version=1,
        status=PlanStatus.active,
        care_recipient_id=payload.care_recipient_id,
        unit_id=payload.unit_id,
        service_timezone=payload.service_timezone,
        recurrence=payload.recurrence,
        day_of_week=payload.day_of_week,
        window_start=payload.window_start,
        window_end=payload.window_end,
        duration_minutes=payload.duration_minutes,
        required_qualifications=payload.required_qualifications,
        created_at=now,
        updated_at=now,
    )
    db.add(plan)
    db.flush()
    for prereq_id in payload.prerequisite_plan_ids:
        db.execute(
            plan_prerequisites.insert().values(plan_id=plan.id, prerequisite_plan_id=prereq_id)
        )
    db.commit()
    db.refresh(plan)
    return plan


def update_plan(db: Session, plan_id: int, payload: PlanUpdate,
                *, actor: str = "coordinator") -> tuple[CarePlan, "GenerateStats"]:
    plan = db.get(CarePlan, plan_id)
    if plan is None:
        raise NotFoundError(f"plan {plan_id} not found")

    if payload.status is not None:
        plan.status = payload.status

    scheduling_fields = [
        payload.service_timezone, payload.recurrence, payload.day_of_week,
        payload.window_start, payload.window_end, payload.duration_minutes,
        payload.required_qualifications,
    ]
    if any(f is not None for f in scheduling_fields):
        plan.service_timezone = payload.service_timezone or plan.service_timezone
        plan.recurrence = payload.recurrence or plan.recurrence
        # day_of_week is nullable and may be explicitly changed; only override
        # when a recurrence change implies it.
        if payload.day_of_week is not None:
            plan.day_of_week = payload.day_of_week
        plan.window_start = payload.window_start or plan.window_start
        plan.window_end = payload.window_end or plan.window_end
        plan.duration_minutes = (
            payload.duration_minutes
            if payload.duration_minutes is not None
            else plan.duration_minutes
        )
        if payload.required_qualifications is not None:
            plan.required_qualifications = payload.required_qualifications
        _validate_windows(
            plan.recurrence, plan.day_of_week, plan.window_start,
            plan.window_end, plan.duration_minutes,
        )
        get_zone(plan.service_timezone)
        plan.version += 1
        plan.updated_at = clock.now()

    if payload.prerequisite_plan_ids is not None:
        _validate_prerequisites(
            db, payload.prerequisite_plan_ids, plan.care_recipient_id
        )
        db.execute(plan_prerequisites.delete().where(plan_prerequisites.c.plan_id == plan.id))
        for prereq_id in payload.prerequisite_plan_ids:
            db.execute(
                plan_prerequisites.insert().values(
                    plan_id=plan.id, prerequisite_plan_id=prereq_id
                )
            )
        plan.version += 1
        plan.updated_at = clock.now()

    db.commit()
    db.refresh(plan)

    # Re-generate so the revision only affects not-yet-started occurrences.
    stats = generate_tasks(db, plan, actor=actor)
    return plan, stats


# --- generation -------------------------------------------------------------


@dataclass
class GenerateStats:
    plan_id: int
    horizon_days: int
    created: int = 0
    updated: int = 0
    skipped_locked: int = 0
    cancelled_stale: int = 0
    tasks: list[Task] = field(default_factory=list)


def _resolve_prerequisites(
    db: Session, plan: CarePlan, local_date,
) -> list[int]:
    """Same-recipient, same-day tasks of prerequisite plans."""
    prereq_pids = prerequisite_plan_ids(db, plan.id)
    if not prereq_pids:
        return []
    key_date = local_date.isoformat()
    rows = db.scalars(
        select(Task).where(
            Task.plan_id.in_(prereq_pids),
            Task.occurrence_key == key_date,
            Task.status != TaskStatus.cancelled,
        )
    ).all()
    return sorted(t.id for t in rows
                  if t.care_recipient_id == plan.care_recipient_id)


def generate_tasks(db: Session, plan: CarePlan, *, actor: str = "system") -> GenerateStats:
    """Create/refresh the next 14 days of tasks for ``plan``.

    Safe to call repeatedly: existing occurrences are reused or updated rather
    than duplicated, and occurrences that fell out of the schedule (e.g. a
    daily plan changed to weekly) are cancelled when still unstarted.
    """
    settings = get_settings()
    horizon = settings.generation_horizon_days
    now = clock.now()
    stats = GenerateStats(plan_id=plan.id, horizon_days=horizon)

    existing = db.scalars(
        select(Task).where(Task.plan_id == plan.id)
    ).all()
    # Includes cancelled rows so a date that re-enters the schedule
    # (e.g. daily -> weekly -> daily) is revived instead of colliding with the
    # (plan_id, occurrence_key) unique constraint.
    index = {(t.plan_id, t.occurrence_key): t for t in existing}

    desired_dates = set()
    fresh_tasks: list[Task] = []

    for local_date in _occurrence_dates(plan, horizon):
        if plan.status == PlanStatus.cancelled:
            break
        desired_dates.add(local_date.isoformat())
        key = local_date.isoformat()
        starts_at, ends_at = _window_for(plan, local_date)
        current = index.get((plan.id, key))

        if current is None:
            task = Task(
                plan_id=plan.id,
                plan_version=plan.version,
                care_recipient_id=plan.care_recipient_id,
                unit_id=plan.unit_id,
                occurrence_key=key,
                starts_at=starts_at,
                ends_at=ends_at,
                status=TaskStatus.pending,
                prerequisite_task_ids=[],
                created_at=now,
                updated_at=now,
            )
            db.add(task)
            db.flush()
            index[(plan.id, key)] = task
            stats.created += 1
            fresh_tasks.append(task)
            stats.tasks.append(task)
            continue

        if current.status in (TaskStatus.assigned, TaskStatus.completed):
            # Work that has started is locked to the old plan version.
            stats.skipped_locked += 1
            stats.tasks.append(current)
            continue

        revived = current.status == TaskStatus.cancelled
        if revived:
            # Date re-entered the schedule: bring the cancelled task back.
            current.status = TaskStatus.pending

        if current.status == TaskStatus.invited:
            offer = db.scalars(
                select(Assignment).where(
                    Assignment.task_id == current.id,
                    Assignment.status == AssignmentStatus.invited,
                )
            ).first()
            if offer is not None:
                _lapse_offer(
                    db, current, offer, actor=actor,
                    reason="plan_revised_before_acceptance",
                )

        changed = (
            current.plan_version != plan.version
            or current.starts_at != starts_at
            or current.ends_at != ends_at
        )
        current.plan_version = plan.version
        current.starts_at = starts_at
        current.ends_at = ends_at
        current.updated_at = now
        if changed or revived:
            stats.updated += 1
        fresh_tasks.append(current)
        stats.tasks.append(current)

    # Cancel unstarted occurrences no longer produced by the schedule.
    for (pid, key), task in index.items():
        if pid != plan.id:
            continue
        if key in desired_dates or task.status in (
            TaskStatus.assigned, TaskStatus.completed, TaskStatus.cancelled
        ):
            continue
        if task.status == TaskStatus.invited:
            offer = db.scalars(
                select(Assignment).where(
                    Assignment.task_id == task.id,
                    Assignment.status == AssignmentStatus.invited,
                )
            ).first()
            if offer is not None:
                _lapse_offer(db, task, offer, actor=actor,
                             reason="occurrence_removed_from_plan")
        task.status = TaskStatus.cancelled
        task.updated_at = now
        db.add(
            AssignmentEvent(
                task_id=task.id,
                action="task_cancelled",
                reason="plan schedule no longer includes this occurrence",
                actor=actor,
                detail={"plan_version": plan.version},
                created_at=now,
            )
        )
        stats.cancelled_stale += 1

    db.flush()

    # Re-resolve prerequisites against the freshly built same-day tasks.
    for task in fresh_tasks:
        local_date = datetime.fromisoformat(task.occurrence_key).date()
        task.prerequisite_task_ids = _resolve_prerequisites(db, plan, local_date)

    db.commit()
    for t in stats.tasks:
        db.refresh(t)
    return stats
