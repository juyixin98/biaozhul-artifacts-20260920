"""Timeout escalation: scheduling, one-shot semantics, restart recovery."""
from __future__ import annotations

import threading
from datetime import timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ScheduledEscalation, utcnow
from app.scheduler import EscalationScheduler
from tests.helpers import create_published, decide, simple_chain_def, start


def test_escalation_reassigns_and_closes_old_tasks(client, req_id):
    create_published(
        client,
        "esc1",
        simple_chain_def(mode="any", assignees=("a1", "a2"), timeout=60, escalate_to=["cfo"]),
    )
    inst = start(client, "esc1")
    iid, ver = inst["id"], inst["version"]

    # force the job due in the past
    db = SessionLocal()
    job = db.scalar(select(ScheduledEscalation).where(ScheduledEscalation.instance_id == iid))
    assert job is not None
    job.due_at = utcnow() - timedelta(seconds=1)
    db.commit(); db.close()

    count = EscalationScheduler().tick()
    assert count == 1

    got = client.get(f"/instances/{iid}").json()
    assert got["current_node_id"] == "ap"
    assert {t["assignee"] for t in got["open_tasks"]} == {"cfo"}
    assert all(t["generation"] == 1 for t in got["open_tasks"])

    hist = client.get(f"/instances/{iid}/history").json()["history"]
    assert any(h["action"] == "tasks_escalated" for h in hist)


def test_escalation_is_one_shot_repeated_tick_is_noop(client):
    create_published(
        client,
        "esc2",
        simple_chain_def(mode="any", assignees=("a1",), timeout=60, escalate_to=["cfo"]),
    )
    inst = start(client, "esc2")
    iid = inst["id"]
    db = SessionLocal()
    db.query(ScheduledEscalation).update({"due_at": utcnow() - timedelta(seconds=1)})
    db.commit(); db.close()

    assert EscalationScheduler().tick() == 1
    # second sweep: the old job is done; the newly armed one is not due yet
    assert EscalationScheduler().tick() == 0

    got = client.get(f"/instances/{iid}").json()
    assert len(got["open_tasks"]) == 1
    assert got["open_tasks"][0]["assignee"] == "cfo"


def test_escalation_after_approval_is_stale(client, req_id):
    create_published(
        client,
        "esc3",
        simple_chain_def(mode="any", assignees=("a1",), timeout=60, escalate_to=["cfo"]),
    )
    inst = start(client, "esc3")
    iid, ver = inst["id"], inst["version"]
    decide(client, instance_id=iid, version=ver, actor="a1",
           decision="approve", request_id=req_id())

    db = SessionLocal()
    due = db.scalars(select(ScheduledEscalation)).all()
    # approval marked the pending job done
    assert all(row.done for row in due)
    db.close()

    assert EscalationScheduler().tick() == 0
    assert client.get(f"/instances/{iid}").json()["status"] == "completed"


def test_scheduler_restart_picks_up_unfinished_job(client):
    """Simulate a restart: a brand-new scheduler instance processes rows that
    were left pending by the previous process."""
    create_published(
        client,
        "esc4",
        simple_chain_def(mode="any", assignees=("a1",), timeout=1, escalate_to=["cfo"]),
    )
    inst = start(client, "esc4")
    iid = inst["id"]

    # make due, then create a fresh scheduler (as after a process restart)
    db = SessionLocal()
    db.query(ScheduledEscalation).update({"due_at": utcnow() - timedelta(seconds=5)})
    db.commit(); db.close()

    fresh = EscalationScheduler(interval_seconds=0.05)
    assert fresh.tick() == 1
    got = client.get(f"/instances/{iid}").json()
    assert {t["assignee"] for t in got["open_tasks"]} == {"cfo"}


def test_concurrent_escalation_and_approval_transition_once(client, req_id):
    create_published(
        client,
        "esc5",
        simple_chain_def(mode="any", assignees=("a1",), timeout=60, escalate_to=["cfo"]),
    )
    inst = start(client, "esc5")
    iid, ver = inst["id"], inst["version"]

    db = SessionLocal()
    db.query(ScheduledEscalation).update({"due_at": utcnow() - timedelta(seconds=1)})
    db.commit(); db.close()

    errors = []

    def approve_worker():
        db = SessionLocal()
        try:
            from app.schemas import DecisionIn
            from app.engine import decide as engine_decide
            engine_decide(db, DecisionIn(
                request_id=f"esc5-approve-{iid}", instance_id=iid,
                expected_version=ver, actor="a1", decision="approve",
            ))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()

    def escalate_worker():
        try:
            EscalationScheduler().tick()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=approve_worker)
    t2 = threading.Thread(target=escalate_worker)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert errors == []
    got = client.get(f"/instances/{iid}").json()
    # either completed via approval, or escalated to cfo — never both/half
    if got["status"] == "completed":
        assert not got["open_tasks"]
    else:
        assert {t["assignee"] for t in got["open_tasks"]} == {"cfo"}
    hist = client.get(f"/instances/{iid}/history").json()["history"]
    assert len([h for h in hist if h["action"] == "tasks_escalated"]) <= 1


def test_failed_job_is_retried_on_next_sweep(client, monkeypatch):
    create_published(
        client,
        "esc6",
        simple_chain_def(mode="any", assignees=("a1",), timeout=60, escalate_to=["cfo"]),
    )
    inst = start(client, "esc6")
    iid = inst["id"]
    db = SessionLocal()
    db.query(ScheduledEscalation).update({"due_at": utcnow() - timedelta(seconds=1)})
    db.commit(); db.close()

    import app.scheduler as sched_mod

    calls = {"n": 0}
    real_run_one = EscalationScheduler._run_one

    def flaky(self, job_id):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate a transient task failure: the job must stay pending.
            raise RuntimeError("transient failure")
        return real_run_one(self, job_id)

    monkeypatch.setattr(EscalationScheduler, "_run_one", flaky)
    scheduler = EscalationScheduler()
    try:
        scheduler.tick()  # first attempt fails before real processing
    except RuntimeError:
        # In the real background thread the loop guard logs this and continues.
        pass
    # the row must still be pending so a restart/next sweep retries it
    db = SessionLocal()
    pending = db.scalar(
        select(ScheduledEscalation).where(
            ScheduledEscalation.instance_id == iid, ScheduledEscalation.done.is_(False)
        )
    )
    db.close()
    assert pending is not None

    EscalationScheduler().tick()  # real retry succeeds
    got = client.get(f"/instances/{iid}").json()
    assert {t["assignee"] for t in got["open_tasks"]} == {"cfo"}
