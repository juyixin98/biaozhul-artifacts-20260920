"""Task generation from care-plan versions.

Rules implemented:

* Generate occurrences for the next ``HORIZON_DAYS`` (14) days starting from
  the generation instant's date in the *plan* timezone.
* Each weekly slot fires on matching weekdays; the plan's ``period_days`` is
  anchored at ``anchor_date`` so e.g. a 7-day plan only fires on the anchor
  weekday. Occurrences whose local date is before ``anchor_date`` are skipped.
* Idempotent: ``(plan_version_id, slot_id, occurrence_date)`` has a unique
  constraint; regenerating the same version never creates duplicate orders.
* Prerequisites: a task remembers which task of each prerequisite plan (same
  occurrence date) must be completed before assignment.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.clock import Clock
from app.enums import EventType, TaskStatus
from app.models import (
    AssignmentEvent,
    CarePlan,
    PlanVersion,
    Task,
    TaskPrerequisite,
    TaskQualification,
    WeeklySlot,
)
from app.services import time_utils
from app.services.errors import NotFoundError, ValidationError
from app.services.time_utils import combine_local, local_date_of


def generate_tasks(
    db: Session,
    *,
    plan_id: int,
    now: datetime,
    horizon_days: int | None = None,
) -> list[Task]:
    """Generate (idempotently) all tasks for a plan's active version.

    Returns the list of tasks for the horizon window — both freshly created
    and pre-existing ones.
    """
    plan = db.get(CarePlan, plan_id)
    if plan is None:
        raise NotFoundError(f"Care plan {plan_id} not found")
    version = plan.active_version()
    if version is None:
        raise ValidationError(f"Care plan {plan_id} has no active version")

    horizon_days = horizon_days or settings.horizon_days
    tz = version.timezone
    start_date = local_date_of(now, tz)
    anchor_local = time_utils.ensure_utc(version.anchor_date).astimezone(
        time_utils.get_zone(tz)
    )
    anchor_date = anchor_local.date()
    end_date = start_date + timedelta(days=horizon_days - 1)

    created: list[Task] = []
    for slot in version.slots:
        occurrence = start_date
        while occurrence <= end_date:
            if (
                occurrence.weekday() == slot.weekday
                and occurrence >= anchor_date
                and (occurrence - anchor_date).days % version.period_days == 0
            ):
                task = _ensure_task(db, plan, version, slot, occurrence, now)
                if task is not None:
                    created.append(task)
            occurrence += timedelta(days=1)

    db.flush()
    _link_prerequisites(db, version, created + _existing_horizon_tasks(
        db, version, start_date, end_date
    ))
    db.flush()
    return _existing_horizon_tasks(db, version, start_date, end_date)


def _ensure_task(
    db: Session,
    plan: CarePlan,
    version: PlanVersion,
    slot: WeeklySlot,
    occurrence: date,
    now: datetime,
) -> Task | None:
    """Create the task for one occurrence unless it already exists.

    Returns the task if created, ``None`` if it was already present (the
    unique key makes regeneration safe even across concurrent calls).
    """
    existing = db.scalar(
        select(Task).where(
            Task.plan_version_id == version.id,
            Task.slot_id == slot.id,
            Task.occurrence_date == occurrence,
        )
    )
    if existing is not None:
        return None

    starts_at = combine_local(occurrence, slot.start_at, version.timezone)
    ends_at = starts_at + timedelta(minutes=slot.duration_minutes)
    latest_start_at = combine_local(
        occurrence, slot.latest_start_at, version.timezone
    )
    if latest_start_at < starts_at:
        # Window end may cross midnight.
        latest_start_at += timedelta(days=1)
    task = Task(
        plan_id=plan.id,
        plan_version_id=version.id,
        slot_id=slot.id,
        occurrence_date=occurrence,
        starts_at=starts_at,
        ends_at=ends_at,
        latest_start_at=latest_start_at,
        duration_minutes=slot.duration_minutes,
        status=TaskStatus.PENDING.value,
        created_at=now,
    )
    db.add(task)
    db.flush()  # assign task.id
    for pq in version.qualifications:
        db.add(
            TaskQualification(task_id=task.id, qualification_id=pq.qualification_id)
        )
    db.add(
        AssignmentEvent(
            task_id=task.id,
            event_type=EventType.TASK_GENERATED,
            detail=f"Generated for {occurrence.isoformat()}",
            created_at=now,
        )
    )
    return task


def _existing_horizon_tasks(
    db: Session, version: PlanVersion, start_date: date, end_date: date
) -> list[Task]:
    stmt = (
        select(Task)
        .where(Task.plan_version_id == version.id)
        .where(Task.occurrence_date >= start_date, Task.occurrence_date <= end_date)
        .order_by(Task.starts_at)
    )
    return list(db.scalars(stmt))


def _link_prerequisites(db: Session, version: PlanVersion, tasks: list[Task]) -> None:
    """Point each task at the prerequisite plans' task on the same date.

    Resolution is best-effort across versions: we look for any non-cancelled
    task of the required plan with the same occurrence date, preferring the
    newest version. Missing prerequisites are picked up by later generation
    runs; the assignment guard also re-checks completion at scheduling time.
    """
    if not version.prerequisites or not tasks:
        return
    for task in tasks:
        for prereq in version.prerequisites:
            required = db.scalar(
                select(Task)
                .where(
                    Task.plan_id == prereq.required_plan_id,
                    Task.occurrence_date == task.occurrence_date,
                    Task.status != TaskStatus.CANCELLED.value,
                )
                .order_by(Task.plan_version_id.desc())
            )
            if required is None:
                # The prerequisite plan may be generated later (or its task
                # cancelled); skip linking now — a subsequent run will link.
                continue
            link_exists = db.scalar(
                select(TaskPrerequisite).where(
                    TaskPrerequisite.task_id == task.id,
                    TaskPrerequisite.required_task_id == required.id,
                )
            )
            if link_exists is None:
                db.add(
                    TaskPrerequisite(task_id=task.id, required_task_id=required.id)
                )
