"""Invitation acceptance: concurrency, idempotency, expiry-vs-accept races."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Thread

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import make_engine
from app.models import (
    Assignment,
    CarePlan,
    Invitation,
    InvitationStatus,
    PlanTaskTemplate,
    Task,
    TaskStatus,
    Unit,
    Worker,
)
from app.services.allocation import (
    AllocationError,
    accept_invitation,
    allocate_task,
    expire_due_invitations,
)
from tests.conftest import make_unit, make_worker

MON = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)


def _task_for_two_workers(db, unit, w1, w2, *, start=MON + timedelta(days=1, hours=10)):
    plan = CarePlan(external_id="P-conc", revision=1, active=True, client_name="C",
                    unit_id=unit.id, timezone="UTC", created_at=MON)
    db.add(plan)
    db.flush()
    tpl = PlanTaskTemplate(plan_id=plan.id, code="V", name="Visit",
                           window_start_minute=0, window_end_minute=1440,
                           duration_minutes=60, weekday_mask=[], qualification_codes=[])
    db.add(tpl)
    db.flush()
    task = Task(plan_id=plan.id, template_id=tpl.id, unit_id=unit.id,
                scheduled_date=start.date().isoformat(),
                scheduled_start=start, scheduled_end=start + timedelta(hours=1),
                status=TaskStatus.PLANNED.value, qualification_codes=[],
                prerequisites=[], created_at=MON)
    db.add(task)
    db.flush()
    return task


def settings() -> Settings:
    return Settings(invitation_ttl_minutes=8)


def test_two_workers_concurrent_accept_only_one_assignment(db: Session):
    """Two accept calls racing concurrently -> exactly one assignment survives."""
    unit = make_unit(db)
    w1 = make_worker(db, unit)
    w2 = make_worker(db, unit)
    task = _task_for_two_workers(db, unit, w1, w2)

    # Create two pending invitations (simulate allocation rounds).
    inv1 = Invitation(task_id=task.id, worker_id=w1.id, round=1,
                      status=InvitationStatus.PENDING.value, created_at=MON,
                      expires_at=MON + timedelta(minutes=8))
    inv2 = Invitation(task_id=task.id, worker_id=w2.id, round=2,
                      status=InvitationStatus.PENDING.value, created_at=MON,
                      expires_at=MON + timedelta(minutes=8))
    db.add_all([inv1, inv2])
    db.commit()

    url = db.bind.url
    engine = make_engine(url.render_as_string(hide_password=False))

    results: list[str] = []

    def accept(inv_id: int, worker_id: int) -> None:
        own = Session(engine)
        try:
            accept_invitation(own, inv_id, worker_id, now=MON + timedelta(minutes=1))
            own.commit()
            results.append(f"ok:{worker_id}")
        except AllocationError as exc:
            own.rollback()
            results.append(f"err:{worker_id}:{exc.code}")
        finally:
            own.close()

    t1 = Thread(target=accept, args=(inv1.id, w1.id))
    t2 = Thread(target=accept, args=(inv2.id, w2.id))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    oks = [r for r in results if r.startswith("ok")]
    errs = [r for r in results if r.startswith("err")]
    assert len(oks) == 1, results
    assert len(errs) == 1 and "already_assigned" in errs[0], results

    # Exactly one assignment row; it holds exactly one task's worth of hours.
    check = Session(engine)
    assert check.scalar(select(func.count()).select_from(Assignment)) == 1
    row = check.scalar(select(Assignment))
    task_row = check.get(Task, task.id)
    assert task_row.status == TaskStatus.ASSIGNED.value
    assert row.worker_id == int(oks[0].split(":")[1])
    # Loser invitation cancelled, winner accepted.
    statuses = {i.worker_id: i.status for i in check.scalars(select(Invitation))}
    assert sorted(statuses.values()) == sorted(
        [InvitationStatus.ACCEPTED.value, InvitationStatus.CANCELLED.value]
    )
    check.close()
    engine.dispose()


def test_duplicate_accept_same_invitation_is_idempotent(db: Session):
    """Repeated accept of the same invitation never consumes hours twice."""
    unit = make_unit(db)
    w = make_worker(db, unit)
    task = _task_for_two_workers(db, unit, w, w)
    inv = Invitation(task_id=task.id, worker_id=w.id, round=1,
                     status=InvitationStatus.PENDING.value, created_at=MON,
                     expires_at=MON + timedelta(minutes=8))
    db.add(inv)
    db.commit()

    a1 = accept_invitation(db, inv.id, w.id, now=MON + timedelta(minutes=1))
    db.commit()
    a2 = accept_invitation(db, inv.id, w.id, now=MON + timedelta(minutes=2))
    db.commit()
    assert a1.id == a2.id
    assert db.scalar(select(func.count()).select_from(Assignment)) == 1


def test_accept_after_expiry_rejected_and_task_reallocated(db: Session):
    unit = make_unit(db)
    w1 = make_worker(db, unit)
    w2 = make_worker(db, unit)
    task = _task_for_two_workers(db, unit, w1, w2)
    result = allocate_task(db, task.id, now=MON, settings=settings())
    assert result.worker_id == w1.id
    db.commit()

    # 9 minutes pass: invitation expires, reaper reallocates to w2.
    later = MON + timedelta(minutes=9)
    affected = expire_due_invitations(db, now=later, settings=settings())
    db.commit()
    assert task.id in affected
    db.expire_all()
    fresh_task = db.get(Task, task.id)
    pending = db.scalars(select(Invitation).where(
        Invitation.task_id == task.id, Invitation.status == InvitationStatus.PENDING.value
    )).all()
    assert len(pending) == 1 and pending[0].worker_id == w2.id
    assert fresh_task.status == TaskStatus.INVITED.value

    # w1's late accept must fail — the expired invitation can no longer bind.
    expired_inv = db.scalar(select(Invitation).where(
        Invitation.task_id == task.id, Invitation.worker_id == w1.id
    ))
    try:
        accept_invitation(db, expired_inv.id, w1.id, now=later)
        assert False, "expected AllocationError"
    except AllocationError as exc:
        assert exc.code == "invitation_expired"
    db.commit()
    assert db.scalar(select(func.count()).select_from(Assignment)) == 0


def test_reaper_and_accept_race_only_one_valid_allocation(db: Session):
    """Expiry sweep and accept happening simultaneously collapse to one winner."""
    unit = make_unit(db)
    w1 = make_worker(db, unit)
    w2 = make_worker(db, unit)
    task = _task_for_two_workers(db, unit, w1, w2)
    result = allocate_task(db, task.id, now=MON, settings=settings())
    inv_id = result.invitation_id
    db.commit()

    url = db.bind.url.render_as_string(hide_password=False)
    engine = make_engine(url)
    at = MON + timedelta(minutes=8, seconds=1)
    outcomes: list[str] = []

    def accept() -> None:
        own = Session(engine)
        try:
            accept_invitation(own, inv_id, w1.id, now=at)
            own.commit()
            outcomes.append("accept-ok")
        except AllocationError as exc:
            own.rollback()
            outcomes.append(f"accept-{exc.code}")
        finally:
            own.close()

    def reap() -> None:
        own = Session(engine)
        try:
            expire_due_invitations(own, now=at, settings=settings())
            own.commit()
            outcomes.append("reap-ok")
        finally:
            own.close()

    t1 = Thread(target=accept)
    t2 = Thread(target=reap)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    check = Session(engine)
    assignments = list(check.scalars(select(Assignment)))
    # Exactly one valid allocation at most; never two workers on one task.
    assert len(assignments) <= 1
    assert len({a.worker_id for a in assignments}) == len(assignments)
    task_row = check.get(Task, task.id)
    assert task_row.status in (TaskStatus.ASSIGNED.value, TaskStatus.INVITED.value)
    if assignments:
        # If accept won, w1 is assigned; any reaper-created invitation for w2
        # must be cancelled, not pending.
        assert assignments[0].worker_id == w1.id
        pending = check.scalars(select(Invitation).where(
            Invitation.task_id == task.id, Invitation.status == InvitationStatus.PENDING.value
        )).all()
        assert pending == []
    check.close()
    engine.dispose()


def test_repeat_reap_is_idempotent(db: Session):
    unit = make_unit(db)
    w = make_worker(db, unit)
    task = _task_for_two_workers(db, unit, w, w)
    allocate_task(db, task.id, now=MON, settings=settings())
    db.commit()
    later = MON + timedelta(minutes=20)
    first = expire_due_invitations(db, now=later, settings=settings())
    db.commit()
    second = expire_due_invitations(db, now=later + timedelta(minutes=1), settings=settings())
    db.commit()
    assert first == [task.id]
    assert second == []
    # Only one invitation round chain per expiry, no duplicate assignments.
    assert db.scalar(select(func.count()).select_from(Assignment)) == 0
