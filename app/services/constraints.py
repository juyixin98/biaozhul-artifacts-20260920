"""Constraint checking and candidate ranking for task allocation.

The rules enforced here are the contract for *every* path that puts a worker
on a task — automatic allocation, manual coordinator assignment and
invitation acceptance:

* qualification valid over the **whole** task interval;
* no time overlap with load-bearing assignments;
* at least 10h rest between adjacent shifts;
* weekly hours <= 44, overnight shifts split at the ISO-week boundary.

Candidates are ranked by remaining weekly availability (primary) and
existing load (secondary), ties broken by worker id for stable ordering.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    LOAD_BEARING_STATUSES,
    Assignment,
    Task,
    Worker,
    WorkerQualification,
)
from app.services.timeutils import REST_REQUIRED, WEEK_CAP, as_utc, iso_week_start, overlaps, split_into_weeks


def load_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Load query window covering BOTH weekly-cap weeks and the 10h rest margins.

    Weekly totals must include every assignment in each touched ISO week
    (not only shifts adjacent to the candidate), so the window spans from
    10h before the first touched week's Monday to 10h after the last touched
    week's end — that also captures every shift relevant to rest/overlap.
    """
    week_begin = iso_week_start(start)
    last_week_end = iso_week_start(end) + timedelta(days=7)
    return week_begin - REST_REQUIRED, last_week_end + REST_REQUIRED

# Constraint code vocabulary returned to API callers.
C_QUALIFICATION = "qualification_not_covered"
C_OVERLAP = "time_overlap"
C_REST = "insufficient_rest"
C_WEEKLY_CAP = "weekly_cap_exceeded"
C_INACTIVE = "worker_inactive"
C_UNIT = "worker_outside_unit"
C_TASK_NOT_OPEN = "task_not_open"
C_INVITATION = "invitation_invalid"

ALL_CONSTRAINTS = (C_QUALIFICATION, C_OVERLAP, C_REST, C_WEEKLY_CAP, C_INACTIVE, C_UNIT)


@dataclass
class Violation:
    constraint: str
    message: str
    worker_id: int | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class Load:
    """A worker's load-bearing assignments relevant to a target interval."""

    assignments: list[Assignment]

    def weekly_minutes(self) -> dict[datetime, float]:
        totals: dict[datetime, float] = {}
        for a in self.assignments:
            t = a.task
            for piece in split_into_weeks(t.scheduled_start, t.scheduled_end):
                totals[piece.week_start] = totals.get(piece.week_start, 0.0) + piece.duration.total_seconds() / 60
        return totals

    def total_minutes(self) -> float:
        return sum(
            (a.task.scheduled_end - a.task.scheduled_start).total_seconds() / 60
            for a in self.assignments
        )


def fetch_load(session: Session, worker_id: int, window_start: datetime, window_end: datetime) -> Load:
    """All load-bearing assignments intersecting the two weeks touching the task."""
    stmt = (
        select(Assignment)
        .join(Task, Assignment.task_id == Task.id)
        .where(
            Assignment.worker_id == worker_id,
            Assignment.status.in_([s.value for s in LOAD_BEARING_STATUSES]),
            Task.scheduled_end > window_start,
            Task.scheduled_start < window_end,
        )
        .order_by(Task.scheduled_start)
    )
    return Load(list(session.scalars(stmt)))


def evaluate_candidate(
    session: Session,
    *,
    worker: Worker,
    task: Task,
    load: Load | None = None,
    exclude_assignment_id: int | None = None,
) -> list[Violation]:
    """Return all constraint violations for putting ``worker`` on ``task``."""
    violations: list[Violation] = []
    start, end = as_utc(task.scheduled_start), as_utc(task.scheduled_end)

    if not worker.active:
        violations.append(Violation(C_INACTIVE, f"worker {worker.id} is inactive", worker.id))

    if load is None:
        search_start, search_end = load_window(start, end)
        load = fetch_load(session, worker.id, search_start, search_end)

    others = [
        a for a in load.assignments
        if exclude_assignment_id is None or a.id != exclude_assignment_id
    ]

    # 1. Qualifications must cover the ENTIRE task interval.
    for code in task.qualification_codes:
        grant = _find_qualification(session, worker.id, code)
        if grant is None:
            violations.append(
                Violation(
                    C_QUALIFICATION,
                    f"worker {worker.id} lacks qualification {code}",
                    worker.id,
                    {"qualification": code, "reason": "missing"},
                )
            )
        else:
            if as_utc(grant.valid_from) > start:
                reason = "not_yet_valid"
                boundary = grant.valid_from
            elif grant.valid_until is not None and as_utc(grant.valid_until) < end:
                reason = "expires_before_end"
                boundary = grant.valid_until
            else:
                reason, boundary = None, None
            if reason:
                violations.append(
                    Violation(
                        C_QUALIFICATION,
                        f"qualification {code} does not cover the whole task interval ({reason})",
                        worker.id,
                        {"qualification": code, "reason": reason, "boundary": boundary.isoformat() if boundary else None},
                    )
                )

    # 2. No overlap.
    for a in others:
        t = a.task
        if overlaps(start, end, as_utc(t.scheduled_start), as_utc(t.scheduled_end)):
            violations.append(
                Violation(
                    C_OVERLAP,
                    f"overlaps task {t.id} ({t.scheduled_start.isoformat()}..{t.scheduled_end.isoformat()})",
                    worker.id,
                    {"conflicting_task_id": t.id},
                )
            )

    # 3. 10h rest between adjacent shifts (gap measured end->start both sides).
    for a in others:
        t = a.task
        other_start, other_end = as_utc(t.scheduled_start), as_utc(t.scheduled_end)
        if other_end <= start:
            gap = start - other_end
            if gap < REST_REQUIRED:
                violations.append(
                    Violation(C_REST, f"only {gap} rest after task {t.id} (need >= 10h)", worker.id,
                              {"conflicting_task_id": t.id, "gap_minutes": gap.total_seconds() / 60})
                )
        elif other_start >= end:
            gap = other_start - end
            if gap < REST_REQUIRED:
                violations.append(
                    Violation(C_REST, f"only {gap} rest before task {t.id} (need >= 10h)", worker.id,
                              {"conflicting_task_id": t.id, "gap_minutes": gap.total_seconds() / 60})
                )

    # 4. Weekly cap, splitting the candidate interval across week boundaries.
    weekly = load.weekly_minutes()
    for piece in split_into_weeks(start, end):
        used = weekly.get(piece.week_start, 0.0)
        used += piece.duration.total_seconds() / 60
        cap_minutes = WEEK_CAP.total_seconds() / 60
        if used > cap_minutes + 1e-6:
            violations.append(
                Violation(
                    C_WEEKLY_CAP,
                    f"week of {piece.week_start.date()} would reach {used / 60:.2f}h > 44h",
                    worker.id,
                    {
                        "week_start": piece.week_start.isoformat(),
                        "used_minutes": used,
                        "cap_minutes": cap_minutes,
                    },
                )
            )

    return violations


def _find_qualification(session: Session, worker_id: int, code: str) -> WorkerQualification | None:
    from app.models import Qualification

    return session.scalar(
        select(WorkerQualification)
        .join(Qualification, Qualification.id == WorkerQualification.qualification_id)
        .where(WorkerQualification.worker_id == worker_id, Qualification.code == code)
    )


@dataclass
class RankedCandidate:
    worker: Worker
    violations: list[Violation]
    # Remaining minutes in the tightest touched week (lower = less attractive).
    min_remaining_minutes: float
    existing_load_minutes: float

    @property
    def feasible(self) -> bool:
        return not self.violations


def rank_candidates(
    session: Session,
    task: Task,
    workers: Iterable[Worker],
    *,
    exclude_workers: set[int] | None = None,
) -> list[RankedCandidate]:
    """Evaluate every worker in the task's unit and return ranked results.

    Sort key (feasible workers first are picked by the allocator):
    most remaining availability in the tightest week, then least existing
    load, then worker id ascending for stable ties.
    """
    start, end = as_utc(task.scheduled_start), as_utc(task.scheduled_end)
    exclude = exclude_workers or set()
    ranked: list[RankedCandidate] = []
    cap_minutes = WEEK_CAP.total_seconds() / 60

    for worker in workers:
        if worker.id in exclude:
            # Already had a non-accepted turn in this search generation; not a
            # feasibility result — omitted from this ranking entirely.
            continue
        if worker.unit_id != task.unit_id:
            ranked.append(RankedCandidate(
                worker,
                [Violation(C_UNIT, f"worker {worker.id} not in unit {task.unit_id}", worker.id)],
                0.0, 0.0,
            ))
            continue

        load = fetch_load(session, worker.id, *load_window(start, end))
        violations = evaluate_candidate(session, worker=worker, task=task, load=load)
        weekly = load.weekly_minutes()
        remainings = []
        for piece in split_into_weeks(start, end):
            remainings.append(cap_minutes - weekly.get(piece.week_start, 0.0))
        ranked.append(RankedCandidate(
            worker=worker,
            violations=violations,
            min_remaining_minutes=min(remainings) if remainings else cap_minutes,
            existing_load_minutes=load.total_minutes(),
        ))

    ranked.sort(
        key=lambda rc: (
            # Feasible first so the ranking also doubles as "who can we invite".
            0 if not rc.violations else 1,
            -rc.min_remaining_minutes,
            rc.existing_load_minutes,
            rc.worker.id,
        )
    )
    return ranked
