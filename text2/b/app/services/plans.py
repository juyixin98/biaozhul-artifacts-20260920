"""Care-plan create / revise service.

Revision semantics (from the domain spec): *plan改版只影响尚未开始的任务*.

On revision we:
1. copy the plan into a new immutable version (new slots / qualifications /
   prerequisites);
2. mark the previous version SUPERSEDED;
3. cancel every old-version task that has not started (PENDING or with an open
   INVITATION) and lies in the current/future horizon — these get regenerated
   from the new version;
4. leave accepted / in-progress / completed tasks untouched (they keep their
   old version, slots and qualifications snapshot).
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import Clock
from app.enums import (
    AssignmentStatus,
    EventType,
    PlanVersionStatus,
    REPLACEABLE_TASK_STATUSES,
    TaskStatus,
)
from app.models import (
    Assignment,
    AssignmentEvent,
    CarePlan,
    PlanPrerequisite,
    PlanQualification,
    PlanVersion,
    Task,
    WeeklySlot,
)
from app.services.errors import NotFoundError, ValidationError
from app.services.generation import generate_tasks


def _validate_slot(slot: dict[str, Any]) -> dict[str, Any]:
    weekday = int(slot["weekday"])
    if not 0 <= weekday <= 6:
        raise ValidationError("weekday must be 0 (Mon) .. 6 (Sun)")
    start_at = _parse_time(slot["start_at"])
    latest_start_at = _parse_time(slot["latest_start_at"])
    duration = int(slot["duration_minutes"])
    if duration <= 0 or duration > 24 * 60:
        raise ValidationError("duration_minutes must be within (0, 1440]")
    if latest_start_at < start_at:
        # Window may cross midnight — allowed.
        pass
    return {
        "weekday": weekday,
        "start_at": start_at,
        "latest_start_at": latest_start_at,
        "duration_minutes": duration,
    }


def _parse_time(value: Any) -> time:
    if isinstance(value, time):
        return value
    try:
        hh, mm = str(value).split(":")
        return time(hour=int(hh), minute=int(mm))
    except (ValueError, AttributeError) as exc:
        raise ValidationError(f"Invalid time {value!r}; use HH:MM") from exc


def create_plan(
    db: Session,
    clock: Clock,
    *,
    unit_id: int,
    title: str,
    timezone: str,
    period_days: int,
    anchor_date: Any,
    slots: list[dict[str, Any]],
    qualification_ids: list[int],
    prerequisite_plan_ids: list[int],
    coordinator_id: int,
) -> CarePlan:
    from app.services.time_utils import get_zone, to_utc
    import datetime as _dt

    get_zone(timezone)  # raises for an unknown timezone
    if period_days < 1:
        raise ValidationError("period_days must be >= 1")
    if not slots:
        raise ValidationError("At least one weekly slot is required")
    norm_slots = [_validate_slot(s) for s in slots]
    now = clock.now()
    db.info["now"] = now

    if isinstance(anchor_date, _dt.date) and not isinstance(anchor_date, _dt.datetime):
        anchor_utc = to_utc(_dt.datetime.combine(anchor_date, time.min), timezone)
    else:
        anchor_utc = anchor_date
        if anchor_utc.tzinfo is None:
            anchor_utc = to_utc(anchor_utc, timezone)

    plan = CarePlan(unit_id=unit_id, title=title, created_at=now)
    db.add(plan)
    db.flush()
    _add_version(
        db,
        plan=plan,
        version_number=1,
        timezone=timezone,
        period_days=period_days,
        anchor_utc=anchor_utc,
        slots=norm_slots,
        qualification_ids=qualification_ids,
        prerequisite_plan_ids=prerequisite_plan_ids,
        change_note="initial version",
        now=now,
    )
    db.add(
        AssignmentEvent(
            task_id=None,
            coordinator_id=coordinator_id,
            event_type=EventType.PLAN_CREATED,
            detail=f"Plan {plan.id} ({title}) created",
            created_at=now,
        )
    )
    db.flush()
    # Generate the first 14 days immediately.
    generate_tasks(db, plan_id=plan.id, now=now)
    db.flush()
    return plan


def revise_plan(
    db: Session,
    clock: Clock,
    *,
    plan_id: int,
    timezone: str | None = None,
    period_days: int | None = None,
    anchor_date: Any = None,
    slots: list[dict[str, Any]] | None = None,
    qualification_ids: list[int] | None = None,
    prerequisite_plan_ids: list[int] | None = None,
    change_note: str = "",
    coordinator_id: int,
) -> CarePlan:
    from app.services.time_utils import get_zone, to_utc
    import datetime as _dt

    plan = db.get(CarePlan, plan_id)
    if plan is None:
        raise NotFoundError(f"Care plan {plan_id} not found")
    old = plan.active_version()
    if old is None:
        raise ValidationError(f"Plan {plan_id} has no active version")

    new_tz = timezone or old.timezone
    get_zone(new_tz)
    new_period = period_days if period_days is not None else old.period_days
    if new_period < 1:
        raise ValidationError("period_days must be >= 1")

    if anchor_date is not None:
        if isinstance(anchor_date, _dt.date) and not isinstance(
            anchor_date, _dt.datetime
        ):
            anchor_utc = to_utc(_dt.datetime.combine(anchor_date, time.min), new_tz)
        elif anchor_date.tzinfo is None:
            anchor_utc = to_utc(anchor_date, new_tz)
        else:
            anchor_utc = anchor_date
    else:
        anchor_utc = old.anchor_date

    if slots is not None:
        if not slots:
            raise ValidationError("At least one weekly slot is required")
        norm_slots = [_validate_slot(s) for s in slots]
    else:
        norm_slots = [
            {
                "weekday": s.weekday,
                "start_at": s.start_at,
                "latest_start_at": s.latest_start_at,
                "duration_minutes": s.duration_minutes,
            }
            for s in old.slots
        ]
    new_qids = (
        qualification_ids
        if qualification_ids is not None
        else [pq.qualification_id for pq in old.qualifications]
    )
    new_prereqs = (
        prerequisite_plan_ids
        if prerequisite_plan_ids is not None
        else [pp.required_plan_id for pp in old.prerequisites]
    )

    now = clock.now()
    db.info["now"] = now
    old.status = PlanVersionStatus.SUPERSEDED.value
    _cancel_replaceable_tasks(db, old, now)
    _add_version(
        db,
        plan=plan,
        version_number=old.version_number + 1,
        timezone=new_tz,
        period_days=new_period,
        anchor_utc=anchor_utc,
        slots=norm_slots,
        qualification_ids=new_qids,
        prerequisite_plan_ids=new_prereqs,
        change_note=change_note or "revised",
        now=now,
    )
    db.add(
        AssignmentEvent(
            coordinator_id=coordinator_id,
            event_type=EventType.PLAN_REVISED,
            detail={
                "plan_id": plan.id,
                "version": old.version_number + 1,
                "note": change_note,
            },
            created_at=now,
        )
    )
    db.flush()
    generate_tasks(db, plan_id=plan.id, now=now)
    db.flush()
    return plan


def _add_version(
    db: Session,
    *,
    plan: CarePlan,
    version_number: int,
    timezone: str,
    period_days: int,
    anchor_utc: datetime,
    slots: list[dict[str, Any]],
    qualification_ids: list[int],
    prerequisite_plan_ids: list[int],
    change_note: str,
    now: datetime,
) -> PlanVersion:
    version = PlanVersion(
        plan_id=plan.id,
        version_number=version_number,
        status=PlanVersionStatus.ACTIVE.value,
        timezone=timezone,
        period_days=period_days,
        anchor_date=anchor_utc,
        created_at=now,
        change_note=change_note,
    )
    db.add(version)
    db.flush()
    for s in slots:
        db.add(WeeklySlot(plan_version_id=version.id, **s))
    for qid in set(qualification_ids):
        db.add(PlanQualification(plan_version_id=version.id, qualification_id=qid))
    for prereq_id in set(prerequisite_plan_ids):
        if prereq_id == plan.id:
            raise ValidationError("A plan cannot be its own prerequisite")
        db.add(
            PlanPrerequisite(
                plan_version_id=version.id, required_plan_id=prereq_id
            )
        )
    db.flush()
    return version


def _cancel_replaceable_tasks(db: Session, version: PlanVersion, now: datetime) -> None:
    """Cancel not-yet-started tasks of a superseded version.

    Protected: ACCEPTED / IN_PROGRESS / COMPLETED (someone is committed or
    the work happened). Replaced: PENDING and INVITED (open invitations are
    voided and audited).
    """
    tasks = db.scalars(
        select(Task).where(Task.plan_version_id == version.id)
    ).all()
    for task in tasks:
        if task.status not in REPLACEABLE_TASK_STATUSES:
            continue
        for asm in task.assignments:
            if asm.status in (
                AssignmentStatus.PENDING.value,
                AssignmentStatus.ACCEPTED.value,
            ):
                asm.status = AssignmentStatus.CANCELLED.value
                asm.responded_at = now
                db.add(
                    AssignmentEvent(
                        task_id=task.id,
                        assignment_id=asm.id,
                        worker_id=asm.worker_id,
                        event_type=EventType.ASSIGNMENT_CANCELLED,
                        detail="Plan revised before task started",
                        created_at=now,
                    )
                )
        task.status = TaskStatus.CANCELLED.value
        db.add(
            AssignmentEvent(
                task_id=task.id,
                event_type=EventType.TASK_CANCELLED,
                detail="Replaced by a newer plan version",
                created_at=now,
            )
        )
