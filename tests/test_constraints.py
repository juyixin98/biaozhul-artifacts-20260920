"""Constraint and allocation tests: weekly cap, qualification validity, rest,
overlap, stable ranking, and unsatisfied-constraint reporting."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Assignment,
    AssignmentStatus,
    CarePlan,
    Invitation,
    InvitationStatus,
    Task,
    TaskStatus,
)
from app.services.allocation import AllocationError, allocate_task
from app.services import generation as gen
from app.config import Settings
from tests.conftest import (
    CNA,
    LIFT,
    grant_qualification,
    make_qualification,
    make_unit,
    make_worker,
)

MON = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)  # Monday UTC


def _settings() -> Settings:
    return Settings(invitation_ttl_minutes=8)


def _make_task(db, unit, *, start: datetime, minutes: int = 60, quals=()):
    """Create a standalone plan/template/task at an arbitrary UTC interval."""
    plan = CarePlan(
        external_id=f"P-{start.timestamp()}-{start.microsecond}",
        revision=1, active=True, client_name="C", unit_id=unit.id,
        timezone="UTC", created_at=MON,
    )
    db.add(plan)
    db.flush()
    from app.models import PlanTaskTemplate
    template = PlanTaskTemplate(
        plan_id=plan.id, code="V", name="Visit",
        window_start_minute=0, window_end_minute=1440,
        duration_minutes=minutes, weekday_mask=[],
        qualification_codes=list(quals),
    )
    db.add(template)
    db.flush()
    task = Task(
        plan_id=plan.id, template_id=template.id, unit_id=unit.id,
        scheduled_date=start.date().isoformat(),
        scheduled_start=start, scheduled_end=start + timedelta(minutes=minutes),
        status=TaskStatus.PLANNED.value, qualification_codes=list(quals),
        prerequisites=[], created_at=MON,
    )
    db.add(task)
    db.flush()
    return task


def _assign_existing(db, worker, task, *, status=AssignmentStatus.ASSIGNED.value):
    inv = Invitation(task_id=task.id, worker_id=worker.id, round=1,
                     status=InvitationStatus.ACCEPTED.value, created_at=MON,
                     expires_at=MON, responded_at=MON)
    db.add(inv)
    db.flush()
    a = Assignment(task_id=task.id, worker_id=worker.id, invitation_id=inv.id,
                   status=status, assigned_at=MON)
    db.add(a)
    task.status = TaskStatus.ASSIGNED.value
    db.flush()
    return a


def test_weekly_cap_blocks_when_44_hours_reached(db: Session):
    unit = make_unit(db)
    worker = make_worker(db, unit)
    from app.models import Qualification
    make_qualification(db, CNA)
    qual = db.scalar(select(Qualification).where(Qualification.code == CNA))
    grant_qualification(db, worker, qual)

    # Existing load: 4 x 11h shifts = 44h in the same ISO week (Mon..Sun).
    for i in range(4):
        day_start = MON + timedelta(days=i, hours=8)
        t = _make_task(db, unit, start=day_start, minutes=11 * 60, quals=(CNA,))
        _assign_existing(db, worker, t)

    # New 1h task in the same week must be blocked by the cap.
    target = _make_task(db, unit, start=MON + timedelta(days=4, hours=6),
                             minutes=60, quals=(CNA,))
    result = allocate_task(db, target.id, now=MON, settings=_settings())
    assert result.status == "unassigned"
    codes = {c["constraint"] for w in result.violations for c in w["constraints"]}
    assert "weekly_cap_exceeded" in codes


def test_overnight_shift_hours_split_across_weeks_for_cap(db: Session):
    unit = make_unit(db)
    worker = make_worker(db, unit)

    # 42h already in week 1; a Sun 22:00->Mon 06:00 (8h) shift would put
    # 2h into week1 => 44h (OK) and 6h into week2.
    existing_start = MON + timedelta(hours=0)
    t = _make_task(db, unit, start=existing_start, minutes=42 * 60)
    _assign_existing(db, worker, t)

    target_start = MON + timedelta(days=6, hours=22)
    target = _make_task(db, unit, start=target_start, minutes=8 * 60)
    result = allocate_task(db, target.id, now=MON, settings=_settings())
    assert result.status == "invited"
    assert result.worker_id == worker.id

    # Extend the existing shift to 43h (keep the assignment so it stays on
    # the worker's load): the candidate's 2h piece in week1 would total 45h.
    t.scheduled_end = t.scheduled_start + timedelta(hours=43)
    db.flush()
    second_inv = db.scalar(select(Invitation).where(Invitation.task_id == target.id))
    db.delete(second_inv)
    target.status = TaskStatus.PLANNED.value
    db.flush()
    result = allocate_task(db, target.id, now=MON, settings=_settings())
    assert result.status == "unassigned"


def test_qualification_missing_and_expiry_must_cover_whole_interval(db: Session):
    unit = make_unit(db)
    qualified = make_worker(db, unit)
    expiring = make_worker(db, unit)
    make_qualification(db, CNA)
    from app.models import Qualification
    qual = db.scalar(select(Qualification).where(Qualification.code == CNA))
    grant_qualification(db, qualified, qual)
    # Expires halfway through the target task.
    target_start = MON + timedelta(days=2, hours=10)
    target = _make_task(db, unit, start=target_start, minutes=120, quals=(CNA,))
    grant_qualification(db, expiring, qual,
                        valid_until=target_start + timedelta(minutes=30))

    result = allocate_task(db, target.id, now=MON, settings=_settings())
    # The fully qualified worker is chosen.
    assert result.status == "invited" and result.worker_id == qualified.id

    # Remove the good worker's qualification and re-run: only expiring remains.
    from app.models import WorkerQualification
    for wq in db.scalars(select(WorkerQualification).where(WorkerQualification.worker_id == qualified.id)):
        db.delete(wq)
    db.delete(db.scalar(select(Invitation).where(Invitation.task_id == target.id)))
    target.status = TaskStatus.UNASSIGNED.value
    db.flush()
    result = allocate_task(db, target.id, now=MON, settings=_settings())
    assert result.status == "unassigned"
    detail = next(c for w in result.violations if w["worker_id"] == expiring.id
                  for c in w["constraints"] if c["constraint"] == "qualification_not_covered")
    assert detail["detail"]["reason"] == "expires_before_end"


def test_overlap_and_ten_hour_rest(db: Session):
    unit = make_unit(db)
    worker = make_worker(db, unit)
    existing = _make_task(db, unit, start=MON + timedelta(hours=8), minutes=60)
    _assign_existing(db, worker, existing)  # 08:00-09:00

    # Overlapping task.
    overlap = _make_task(db, unit, start=MON + timedelta(hours=8, minutes=30), minutes=60)
    result = allocate_task(db, overlap.id, now=MON, settings=_settings())
    codes = {c["constraint"] for w in result.violations for c in w["constraints"]}
    assert "time_overlap" in codes

    # 9h rest (need 10).
    soon = _make_task(db, unit, start=MON + timedelta(hours=18), minutes=60)
    result = allocate_task(db, soon.id, now=MON, settings=_settings())
    codes = {c["constraint"] for w in result.violations for c in w["constraints"]}
    assert "insufficient_rest" in codes

    # Exactly 10h gap is feasible.
    fine = _make_task(db, unit, start=MON + timedelta(hours=19), minutes=60)
    result = allocate_task(db, fine.id, now=MON, settings=_settings())
    assert result.status == "invited" and result.worker_id == worker.id


def test_candidate_ranking_remaining_capacity_then_load_then_id(db: Session):
    unit = make_unit(db)
    w1 = make_worker(db, unit)  # lower id, no load
    w2 = make_worker(db, unit)  # carries an 8h shift in the same week -> less remaining capacity
    busy_task = _make_task(db, unit, start=MON + timedelta(days=2, hours=8), minutes=8 * 60)
    _assign_existing(db, w2, busy_task)

    target = _make_task(db, unit, start=MON + timedelta(days=3, hours=10), minutes=60)
    result = allocate_task(db, target.id, now=MON, settings=_settings())
    # w2 has only 36h left in that week vs 44h for w1 -> most-remaining wins.
    assert result.worker_id == w1.id


def test_ties_broken_by_worker_id(db: Session):
    unit = make_unit(db)
    w1 = make_worker(db, unit)
    w2 = make_worker(db, unit)
    target = _make_task(db, unit, start=MON + timedelta(days=1, hours=10), minutes=60)
    result = allocate_task(db, target.id, now=MON, settings=_settings())
    assert result.worker_id == w1.id


def test_worker_outside_unit_never_candidate(db: Session):
    unit_a = make_unit(db, "A")
    unit_b = make_unit(db, "B")
    outsider = make_worker(db, unit_b)
    target = _make_task(db, unit_a, start=MON + timedelta(hours=10), minutes=60)
    result = allocate_task(db, target.id, now=MON, settings=_settings())
    assert result.status == "unassigned"
    assert result.violations[0]["worker_id"] == outsider.id
    assert result.violations[0]["constraints"][0]["constraint"] == "worker_outside_unit"


def test_inactive_worker_excluded(db: Session):
    unit = make_unit(db)
    make_worker(db, unit, active=False)
    target = _make_task(db, unit, start=MON + timedelta(hours=10), minutes=60)
    result = allocate_task(db, target.id, now=MON, settings=_settings())
    assert result.status == "unassigned"
    assert result.violations[0]["constraints"][0]["constraint"] == "worker_inactive"
