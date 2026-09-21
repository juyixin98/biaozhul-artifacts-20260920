"""Constraint checking: qualification coverage, overlap, rest, weekly cap."""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import AssignmentStatus, TaskStatus
from app.services.constraints import check_constraints, rank_candidates

UTC = timezone.utc


def _task_with_codes(task):
    # check_constraints reads codes from the plan relation; fixtures already
    # attach the plan via plan_id, so nothing extra is needed.
    return task


def test_qualification_missing_blocks(db, make_unit, make_worker, make_plan,
                                      make_task):
    unit = make_unit()
    plan = make_plan(unit, qualifications=["personal_care"])
    worker = make_worker(unit)  # no qualifications
    task = make_task(
        plan,
        start=datetime(2026, 9, 22, 8, tzinfo=UTC),
        end=datetime(2026, 9, 22, 9, tzinfo=UTC),
    )
    codes = {v.code for v in check_constraints(db, worker, task)}
    assert "qualification_missing_or_expired" in codes


def test_qualification_must_cover_whole_task(db, make_unit, make_worker,
                                             make_plan, make_task):
    unit = make_unit()
    plan = make_plan(unit, qualifications=["personal_care"])
    task = make_task(
        plan,
        start=datetime(2026, 9, 22, 8, tzinfo=UTC),
        end=datetime(2026, 9, 22, 10, tzinfo=UTC),
    )
    # Valid until 09:00 — expires in the middle of the 08:00-10:00 task.
    worker = make_worker(
        unit,
        qualifications=[(
            "personal_care",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
        )],
    )
    codes = {v.code for v in check_constraints(db, worker, task)}
    assert "qualification_missing_or_expired" in codes

    # Extending coverage to task end clears the violation.
    worker.qualifications[0].valid_until = datetime(2026, 9, 22, 10, tzinfo=UTC)
    db.flush()
    assert check_constraints(db, worker, task) == []


def test_overlap_detected(db, make_unit, make_worker, make_plan, make_task,
                          make_assignment):
    unit = make_unit()
    plan1 = make_plan(unit)
    plan2 = make_plan(unit)
    worker = make_worker(unit)
    existing = make_task(
        plan1,
        start=datetime(2026, 9, 22, 8, tzinfo=UTC),
        end=datetime(2026, 9, 22, 10, tzinfo=UTC),
        occurrence="2026-09-22",
    )
    make_assignment(existing, worker, status=AssignmentStatus.assigned)
    candidate = make_task(
        plan2,
        start=datetime(2026, 9, 22, 9, 30, tzinfo=UTC),
        end=datetime(2026, 9, 22, 11, tzinfo=UTC),
        occurrence="2026-09-22-x",
    )
    codes = {v.code for v in check_constraints(db, worker, candidate)}
    assert "overlap" in codes


def test_ten_hour_rest_required(db, make_unit, make_worker, make_plan,
                                make_task, make_assignment):
    unit = make_unit()
    plan1 = make_plan(unit)
    plan2 = make_plan(unit)
    worker = make_worker(unit)
    evening = make_task(
        plan1,
        start=datetime(2026, 9, 22, 20, tzinfo=UTC),
        end=datetime(2026, 9, 22, 22, tzinfo=UTC),
        occurrence="2026-09-22",
    )
    make_assignment(evening, worker, status=AssignmentStatus.assigned)
    # Next morning 06:00 is only 8 hours after 22:00 end.
    too_early = make_task(
        plan2,
        start=datetime(2026, 9, 23, 6, tzinfo=UTC),
        end=datetime(2026, 9, 23, 7, tzinfo=UTC),
        occurrence="2026-09-23",
    )
    codes = {v.code for v in check_constraints(db, worker, too_early)}
    assert "insufficient_rest" in codes

    # 08:00 gives exactly 10h and is allowed.
    ok = make_task(
        plan2,
        start=datetime(2026, 9, 23, 8, tzinfo=UTC),
        end=datetime(2026, 9, 23, 9, tzinfo=UTC),
        occurrence="2026-09-23b",
    )
    assert check_constraints(db, worker, ok) == []


def test_weekly_cap_44h_with_cross_midnight_split(db, make_unit, make_worker,
                                                  make_plan, make_task,
                                                  make_assignment):
    """A shift crossing Monday 00:00 is split across two ISO weeks."""
    unit = make_unit()
    plan = make_plan(unit)
    worker = make_worker(unit, tz="UTC")

    # Book 43h55m already in the Monday-2026-09-21 week.
    booked = make_task(
        plan,
        start=datetime(2026, 9, 21, 12, tzinfo=UTC),
        end=datetime(2026, 9, 23, 7, 55, tzinfo=UTC),
        occurrence="2026-09-21big",
    )
    make_assignment(booked, worker, status=AssignmentStatus.assigned)

    # A 15-minute shift on Sunday 2026-09-27 23:30 lands fully in week 1:
    # 43h55 + 15m = 44h10 > cap.
    end_of_week = make_task(
        plan,
        start=datetime(2026, 9, 27, 23, 30, tzinfo=UTC),
        end=datetime(2026, 9, 27, 23, 45, tzinfo=UTC),
        occurrence="2026-09-27",
    )
    codes = {v.code for v in check_constraints(db, worker, end_of_week)}
    assert "weekly_cap_exceeded" in codes

    # Same 15m crossing midnight splits 10/5: week1 = 44h05 (over),
    # week2 gets 5m alone fine; violation is reported for week1 only.
    crossing = make_task(
        plan,
        start=datetime(2026, 9, 27, 23, 50, tzinfo=UTC),
        end=datetime(2026, 9, 28, 0, 5, tzinfo=UTC),
        occurrence="2026-09-28",
    )
    violations = check_constraints(db, worker, crossing)
    assert len([v for v in violations if v.code == "weekly_cap_exceeded"]) == 1
    detail = next(v.detail for v in violations if v.code == "weekly_cap_exceeded")
    assert detail["booked_minutes"] + detail["shift_minutes"] == 43 * 60 + 55 + 10

    # A task entirely in the fresh week fits.
    fresh_week = make_task(
        plan,
        start=datetime(2026, 9, 28, 12, tzinfo=UTC),
        end=datetime(2026, 9, 28, 13, tzinfo=UTC),
        occurrence="2026-09-28b",
    )
    assert check_constraints(db, worker, fresh_week) == []


def test_invited_shifts_count_toward_cap(db, make_unit, make_worker, make_plan,
                                         make_task, make_assignment):
    unit = make_unit()
    plan1 = make_plan(unit)
    plan2 = make_plan(unit)
    worker = make_worker(unit)
    invited = make_task(
        plan1,
        start=datetime(2026, 9, 21, 8, tzinfo=UTC),
        end=datetime(2026, 9, 22, 20, tzinfo=UTC),  # 36h booked via outstanding offer
        occurrence="2026-09-21",
    )
    make_assignment(invited, worker, status=AssignmentStatus.invited)
    candidate = make_task(
        plan2,
        start=datetime(2026, 9, 23, 8, tzinfo=UTC),
        end=datetime(2026, 9, 23, 17, tzinfo=UTC),  # 9h more -> 45h
        occurrence="2026-09-23",
    )
    codes = {v.code for v in check_constraints(db, worker, candidate)}
    assert "weekly_cap_exceeded" in codes


def test_worker_unit_must_match_task(db, make_unit, make_worker, make_plan,
                                     make_task):
    unit_a = make_unit("A")
    unit_b = make_unit("B")
    plan = make_plan(unit_b)
    worker = make_worker(unit_a)
    task = make_task(
        plan,
        start=datetime(2026, 9, 22, 8, tzinfo=UTC),
        end=datetime(2026, 9, 22, 9, tzinfo=UTC),
    )
    codes = {v.code for v in check_constraints(db, worker, task)}
    assert "unit_mismatch" in codes


def test_ranking_prefers_more_remaining_then_id(db, make_unit, make_worker,
                                                make_plan, make_task,
                                                make_assignment):
    """Worker with more remaining weekly slack wins; id breaks ties."""
    unit = make_unit()
    plan_existing = make_plan(unit)
    plan_task = make_plan(unit)
    w1 = make_worker(unit, name="w1")
    w2 = make_worker(unit, name="w2")  # same load as w1 -> lower id wins

    task = make_task(
        plan_task,
        start=datetime(2026, 9, 22, 8, tzinfo=UTC),
        end=datetime(2026, 9, 22, 9, tzinfo=UTC),
    )
    ranked = rank_candidates(db, [w1, w2], task)
    assert [w.id for w, _ in ranked] == [w1.id, w2.id]

    # Give w2 more remaining slack by loading w1 with 10h this week.
    loaded = make_task(
        plan_existing,
        start=datetime(2026, 9, 21, 8, tzinfo=UTC),
        end=datetime(2026, 9, 21, 18, tzinfo=UTC),
        occurrence="2026-09-21",
    )
    make_assignment(loaded, w1, status=AssignmentStatus.assigned)
    ranked = rank_candidates(db, [w1, w2], task)
    assert ranked[0][0].id == w2.id
