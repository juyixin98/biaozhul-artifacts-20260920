"""Hard scheduling constraints.

Every candidate worker for a task interval is checked against the same set of
rules, whether the assignment originates from automatic scheduling, a manual
coordinator action or an invitation being re-issued:

1. unit membership / active worker;
2. required qualifications valid for the *whole* interval;
3. no time overlap with another live assignment or open invitation;
4. at least ``REST_BETWEEN_SHIFTS_HOURS`` between adjacent shifts;
5. weekly worked time stays <= ``WEEKLY_HOUR_LIMIT`` in every ISO week the
   (possibly midnight-crossing) shift touches.

Open (not-yet-expired) invitations are treated as provisional bookings for
rules 3–5: two workers must never be tentatively committed to overlapping
slots, and accepting an invitation can never silently push someone over the
weekly cap. Expired/declined/cancelled assignments are ignored.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.enums import AssignmentStatus
from app.models import (
    Assignment,
    Qualification,
    Task,
    TaskQualification,
    Worker,
    WorkerQualification,
    WorkerUnit,
)
from app.services import time_utils
from app.services.time_utils import WeekSlice

# Assignment statuses that occupy a worker's calendar.
LIVE_STATUSES = (AssignmentStatus.PENDING.value, AssignmentStatus.ACCEPTED.value)


@dataclass
class Violation:
    code: str
    message: str
    # Optional machine-readable context, e.g. {"qualification": "BLS"}.
    context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.context:
            out["context"] = self.context
        return out


@dataclass
class CandidateEvaluation:
    worker_id: int
    worker_name: str
    feasible: bool
    violations: list[Violation] = field(default_factory=list)
    # Ranking metrics (populated for feasible candidates).
    min_remaining_minutes: int = 0
    total_load_minutes: int = 0
    weeks: dict[date, int] = field(default_factory=dict)

    def violation_dicts(self) -> list[dict]:
        return [v.to_dict() for v in self.violations]


def _live_assignments_for_workers(
    db: Session, worker_ids: Iterable[int]
) -> list[Assignment]:
    worker_ids = list(worker_ids)
    if not worker_ids:
        return []
    stmt = (
        select(Assignment)
        .where(Assignment.worker_id.in_(worker_ids))
        .where(Assignment.status.in_(LIVE_STATUSES))
    )
    return list(db.scalars(stmt))


def _worker_minutes_by_week(
    assignments: Iterable[Assignment], tz_name: str
) -> dict[date, int]:
    """Sum existing live load per ISO-week Monday for one worker."""
    weeks: dict[date, int] = {}
    for asm in assignments:
        for slice_ in time_utils.split_interval_by_iso_week(
            asm.task_starts_at, asm.task_ends_at, tz_name
        ):
            weeks[slice_.week_start] = weeks.get(slice_.week_start, 0) + int(
                slice_.duration.total_seconds() // 60
            )
    return weeks


def _overlaps(start_a, end_a, start_b, end_b) -> bool:
    return start_a < end_b and start_b < end_a


def evaluate_candidate(
    db: Session,
    *,
    worker: Worker,
    task: Task,
    required_qualification_ids: list[int],
    starts_at=None,
    ends_at=None,
    exclude_assignment_id: int | None = None,
    now=None,
) -> CandidateEvaluation:
    """Check one worker against every hard rule for the given interval.

    ``starts_at``/``ends_at`` default to the task's stored interval but may be
    overridden (manual reschedule). ``exclude_assignment_id`` lets a manual
    edit ignore the assignment being replaced.
    """
    starts_at = time_utils.ensure_utc(starts_at or task.starts_at)
    ends_at = time_utils.ensure_utc(ends_at or task.ends_at)
    tz_name = worker.timezone or "UTC"
    violations: list[Violation] = []

    # --- Rule 0: active worker ------------------------------------------------
    if not worker.active:
        violations.append(
            Violation("worker_inactive", f"Worker {worker.name} is inactive")
        )

    # --- Rule 1: qualifications cover the whole interval ---------------------
    for qid in required_qualification_ids:
        cred = db.scalar(
            select(WorkerQualification).where(
                WorkerQualification.worker_id == worker.id,
                WorkerQualification.qualification_id == qid,
            )
        )
        qual = db.get(Qualification, qid)
        label = qual.code if qual else f"qual-{qid}"
        if cred is None or cred.revoked:
            violations.append(
                Violation(
                    "missing_qualification",
                    f"Worker lacks required qualification {label}",
                    {"qualification": label},
                )
            )
            continue
        valid_from = time_utils.ensure_utc(cred.valid_from)
        if valid_from > starts_at:
            violations.append(
                Violation(
                    "qualification_not_yet_valid",
                    f"Qualification {label} is only valid from {valid_from.isoformat()}",
                    {"qualification": label, "valid_from": valid_from.isoformat()},
                )
            )
        if cred.valid_until is not None:
            valid_until = time_utils.ensure_utc(cred.valid_until)
            if valid_until < ends_at:
                violations.append(
                    Violation(
                        "qualification_expired",
                        f"Qualification {label} expires {valid_until.isoformat()} "
                        "before the task ends",
                        {
                            "qualification": label,
                            "valid_until": valid_until.isoformat(),
                        },
                    )
                )

    # --- Existing live load --------------------------------------------------
    assignments = [
        a
        for a in _live_assignments_for_workers(db, [worker.id])
        if a.id != exclude_assignment_id
    ]

    # Rule 2: no overlap.
    for other in assignments:
        o_start = time_utils.ensure_utc(other.task_starts_at)
        o_end = time_utils.ensure_utc(other.task_ends_at)
        if _overlaps(starts_at, ends_at, o_start, o_end):
            violations.append(
                Violation(
                    "time_overlap",
                    f"Overlaps another {other.status.value} assignment "
                    f"({o_start.isoformat()}–{o_end.isoformat()})",
                    {
                        "assignment_id": other.id,
                        "starts_at": o_start.isoformat(),
                        "ends_at": o_end.isoformat(),
                    },
                )
            )

    # Rule 3: minimum rest between shifts (gap must be >= 10h on both sides).
    rest = timedelta(hours=settings.rest_between_shifts_hours)
    for other in assignments:
        o_start = time_utils.ensure_utc(other.task_starts_at)
        o_end = time_utils.ensure_utc(other.task_ends_at)
        if _overlaps(starts_at, ends_at, o_start, o_end):
            continue  # already reported as overlap
        if o_end <= starts_at < o_end + rest:
            violations.append(
                Violation(
                    "insufficient_rest",
                    f"Only {_human_minutes((starts_at - o_end))} rest after the "
                    f"previous shift; need {settings.rest_between_shifts_hours}h",
                    {
                        "assignment_id": other.id,
                        "required_rest_minutes": int(rest.total_seconds() // 60),
                    },
                )
            )
        elif o_start >= ends_at and o_start < ends_at + rest:
            violations.append(
                Violation(
                    "insufficient_rest",
                    f"Only {_human_minutes((o_start - ends_at))} rest before the "
                    f"next shift; need {settings.rest_between_shifts_hours}h",
                    {
                        "assignment_id": other.id,
                        "required_rest_minutes": int(rest.total_seconds() // 60),
                    },
                )
            )

    # Rule 4: weekly hour cap, with the candidate interval split by ISO week.
    existing_weeks = _worker_minutes_by_week(assignments, tz_name)
    candidate_slices = time_utils.split_interval_by_iso_week(
        starts_at, ends_at, tz_name
    )
    candidate_weeks: dict[date, int] = {}
    for slice_ in candidate_slices:
        candidate_weeks[slice_.week_start] = candidate_weeks.get(
            slice_.week_start, 0
        ) + int(slice_.duration.total_seconds() // 60)

    cap_minutes = settings.weekly_hour_limit * 60
    projected_weeks: dict[date, int] = dict(existing_weeks)
    for week_start, mins in candidate_weeks.items():
        projected_weeks[week_start] = projected_weeks.get(week_start, 0) + mins
    for week_start, total in projected_weeks.items():
        if week_start in candidate_weeks and total > cap_minutes:
            violations.append(
                Violation(
                    "weekly_hours_exceeded",
                    f"Week of {week_start.isoformat()} would reach "
                    f"{total / 60:.1f}h; limit is {settings.weekly_hour_limit}h",
                    {
                        "week_start": week_start.isoformat(),
                        "projected_minutes": total,
                        "limit_minutes": cap_minutes,
                    },
                )
            )

    eval_ = CandidateEvaluation(
        worker_id=worker.id,
        worker_name=worker.name,
        feasible=not violations,
        violations=violations,
    )
    if eval_.feasible:
        # Ranking metrics:
        #  - min remaining capacity across touched weeks (higher = better):
        #    the worker with the *least* slack in their tightest week first;
        #  - total existing load (lower = better);
        #  - worker id (stable tie-break).
        remaining = {
            week_start: cap_minutes - projected_weeks.get(week_start, 0)
            for week_start in candidate_weeks
        }
        eval_.min_remaining_minutes = min(remaining.values()) if remaining else cap_minutes
        eval_.total_load_minutes = sum(existing_weeks.values())
        eval_.weeks = projected_weeks
    return eval_


def _human_minutes(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}m"


def candidate_pool_for_unit(db: Session, unit_id: int) -> list[Worker]:
    """Active workers belonging to the plan's unit, ordered by id."""
    stmt = (
        select(Worker)
        .join(WorkerUnit, WorkerUnit.worker_id == Worker.id)
        .where(WorkerUnit.unit_id == unit_id, Worker.active.is_(True))
        .order_by(Worker.id)
    )
    return list(db.scalars(stmt))


def rank_candidates(evaluations: list[CandidateEvaluation]) -> list[CandidateEvaluation]:
    """Order feasible workers by the documented ranking key.

    1. smallest remaining slack in the tightest touched week (pack tight
       first, leaving maximally-slack workers for harder future tasks);
    2. smallest existing total load;
    3. worker id ascending (stable, deterministic tie-break).
    """
    feasible = [e for e in evaluations if e.feasible]
    return sorted(
        feasible,
        key=lambda e: (e.min_remaining_minutes, e.total_load_minutes, e.worker_id),
    )
