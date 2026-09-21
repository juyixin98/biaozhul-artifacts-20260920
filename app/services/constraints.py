"""Constraint checking and candidate ranking.

Every booking path (automatic offer, coordinator assignment, accept-time
recheck) passes through :func:`check_constraints`, so manual adjustments can
never bypass the rules. Constraints:

* worker active and belongs to the task's unit
* each required qualification valid across the *whole* task interval
* no overlap with another live (invited/assigned) shift
* at least ``rest_between_shifts_hours`` between adjacent shifts
* at most ``weekly_hour_cap`` minutes in any ISO week the shift touches,
  measured in the worker's timezone (cross-midnight shifts are split across
  the two local ISO weeks)
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    Assignment,
    AssignmentStatus,
    CarePlan,
    CareWorker,
    Qualification,
    Task,
)
from app.timeutils import get_zone, split_minutes_by_week

LIVE_STATUSES = (AssignmentStatus.invited, AssignmentStatus.assigned)


@dataclass
class Violation:
    code: str
    message: str
    detail: dict


def required_codes_for_task(db: Session, task: Task) -> set[str]:
    return set(
        db.get(CarePlan, task.plan_id).required_qualifications  # type: ignore[union-attr]
    )


def _live_intervals(
    db: Session, worker_id: int, exclude_assignment_id: int | None
) -> list[tuple]:
    """[(start, end, assignment_id)] for the worker's live shifts."""
    stmt = (
        select(Assignment, Task.starts_at, Task.ends_at)
        .join(Task, Task.id == Assignment.task_id)
        .where(Assignment.worker_id == worker_id, Assignment.status.in_(LIVE_STATUSES))
    )
    if exclude_assignment_id is not None:
        stmt = stmt.where(Assignment.id != exclude_assignment_id)
    return [(s, e, a.id) for a, s, e in db.execute(stmt).all()]


def _booked_minutes_by_week(
    worker: CareWorker, intervals: list[tuple]
) -> dict[tuple[int, int], int]:
    zone = get_zone(worker.timezone)
    totals: dict[tuple[int, int], int] = {}
    for s, e, _aid in intervals:
        for sl in split_minutes_by_week(s, e, zone):
            totals[sl.week] = totals.get(sl.week, 0) + sl.minutes
    return totals


def check_constraints(
    db: Session,
    worker: CareWorker,
    task: Task,
    *,
    exclude_assignment_id: int | None = None,
    ignore_unit: bool = False,
) -> list[Violation]:
    """Return every violated constraint; empty list means the booking is legal."""
    settings = get_settings()
    violations: list[Violation] = []

    if not worker.active:
        violations.append(
            Violation("worker_inactive", f"Worker {worker.id} is inactive", {"worker_id": worker.id})
        )
    if not ignore_unit and worker.unit_id != task.unit_id:
        violations.append(
            Violation(
                "unit_mismatch",
                f"Worker belongs to unit {worker.unit_id}, task belongs to {task.unit_id}",
                {"worker_unit": worker.unit_id, "task_unit": task.unit_id},
            )
        )

    # Qualifications must cover the whole shift: valid_from <= start and
    # valid_until >= end. One expiring mid-shift does not satisfy the rule.
    required = required_codes_for_task(db, task)
    if required:
        quals = list(
            db.scalars(select(Qualification).where(Qualification.worker_id == worker.id))
        )
        missing: list[str] = []
        for code in sorted(required):
            ok = any(
                q.code == code
                and q.valid_from <= task.starts_at
                and q.valid_until >= task.ends_at
                for q in quals
            )
            if not ok:
                missing.append(code)
        if missing:
            violations.append(
                Violation(
                    "qualification_missing_or_expired",
                    f"Worker lacks qualifications valid for the whole task: {', '.join(missing)}",
                    {"missing": missing,
                     "starts_at": task.starts_at.isoformat(),
                     "ends_at": task.ends_at.isoformat()},
                )
            )

    intervals = _live_intervals(db, worker.id, exclude_assignment_id)
    rest_seconds = settings.rest_between_shifts_hours * 3600

    for s, e, aid in intervals:
        if task.starts_at < e and s < task.ends_at:
            violations.append(
                Violation(
                    "overlap",
                    f"Overlaps live assignment {aid}",
                    {"assignment_id": aid, "start": s.isoformat(), "end": e.isoformat()},
                )
            )
            continue
        gap = (
            (task.starts_at - e).total_seconds()
            if e <= task.starts_at
            else (s - task.ends_at).total_seconds()
        )
        if gap < rest_seconds:
            violations.append(
                Violation(
                    "insufficient_rest",
                    f"Only {int(gap // 60)}m rest against assignment {aid}; "
                    f"need {settings.rest_between_shifts_hours}h",
                    {"assignment_id": aid, "gap_minutes": int(gap // 60),
                     "required_minutes": settings.rest_between_shifts_hours * 60},
                )
            )

    cap_minutes = settings.weekly_hour_cap * 60
    booked = _booked_minutes_by_week(worker, intervals)
    for sl in split_minutes_by_week(task.starts_at, task.ends_at, get_zone(worker.timezone)):
        total = booked.get(sl.week, 0) + sl.minutes
        if total > cap_minutes:
            violations.append(
                Violation(
                    "weekly_cap_exceeded",
                    f"Week {sl.week[0]}-W{sl.week[1]:02d}: {total}m > cap {cap_minutes}m",
                    {"week_year": sl.week[0], "week": sl.week[1],
                     "booked_minutes": booked.get(sl.week, 0),
                     "shift_minutes": sl.minutes, "cap_minutes": cap_minutes},
                )
            )

    return violations


@dataclass
class CandidateScore:
    worker_id: int
    feasible: bool
    remaining_minutes: int
    load_minutes: int
    violations: list[Violation]


def rank_candidates(
    db: Session, workers: list[CareWorker], task: Task
) -> list[tuple[CareWorker, CandidateScore]]:
    """Feasible first; then most remaining slack in the tightest week the shift
    touches; then least existing load; then worker id for a stable order."""
    settings = get_settings()
    cap_minutes = settings.weekly_hour_cap * 60

    scored: list[tuple[CareWorker, CandidateScore]] = []
    for worker in workers:
        violations = check_constraints(db, worker, task)
        intervals = _live_intervals(db, worker.id, None)
        booked = _booked_minutes_by_week(worker, intervals)
        load = sum(booked.values())
        wanted = split_minutes_by_week(
            task.starts_at, task.ends_at, get_zone(worker.timezone)
        )
        remaining = min(cap_minutes - (booked.get(sl.week, 0) + sl.minutes) for sl in wanted)
        scored.append(
            (
                worker,
                CandidateScore(worker.id, not violations, remaining, load, violations),
            )
        )

    scored.sort(
        key=lambda pair: (
            0 if pair[1].feasible else 1,
            -pair[1].remaining_minutes,
            pair[1].load_minutes,
            pair[0].id,
        )
    )
    return scored
