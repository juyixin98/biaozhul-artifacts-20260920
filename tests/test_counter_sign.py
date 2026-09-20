"""Counter-sign (all) and any-sign semantics, including concurrent races."""
from __future__ import annotations

import threading

from tests.helpers import create_published, decide, simple_chain_def, start


def test_all_sign_waits_then_advances(client, req_id):
    create_published(client, "all", simple_chain_def(mode="all", assignees=("a1", "a2")))
    inst = start(client, "all")
    iid, ver = inst["id"], inst["version"]

    r = decide(client, instance_id=iid, version=ver, actor="a1",
               decision="approve", request_id=req_id())
    assert r.status_code == 200
    assert r.json()["result"] == "waiting_for_others"
    assert client.get(f"/instances/{iid}").json()["status"] == "running"

    r = decide(client, instance_id=iid, version=ver, actor="a2",
               decision="approve", request_id=req_id())
    assert r.status_code == 200
    assert r.json()["result"] == "approved_advanced"
    assert r.json()["instance"]["status"] == "completed"


def test_all_sign_one_rejection_terminates_and_closes_others(client, req_id):
    create_published(client, "allr", simple_chain_def(mode="all", assignees=("a1", "a2", "a3")))
    inst = start(client, "allr")
    iid, ver = inst["id"], inst["version"]

    r = decide(client, instance_id=iid, version=ver, actor="a2",
               decision="reject", request_id=req_id(), comment="预算不足")
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "rejected"
    assert body["instance"]["status"] == "rejected"
    assert "预算不足" in body["instance"]["reject_reason"]

    got = client.get(f"/instances/{iid}").json()
    assert not got["open_tasks"]
    # later approve on the closed todo must not change anything
    r = decide(client, instance_id=iid, version=ver, actor="a1",
               decision="approve", request_id=req_id())
    assert r.status_code == 409
    assert r.json()["error"]["code"] in ("task_already_handled", "instance_not_running")
    assert client.get(f"/instances/{iid}").json()["status"] == "rejected"


def test_any_sign_first_approval_advances_and_closes_other_todos(client, req_id):
    create_published(client, "any", simple_chain_def(mode="any", assignees=("a1", "a2", "a3")))
    inst = start(client, "any")
    iid, ver = inst["id"], inst["version"]

    r = decide(client, instance_id=iid, version=ver, actor="a2",
               decision="approve", request_id=req_id())
    assert r.status_code == 200
    assert r.json()["result"] == "approved_advanced"

    got = client.get(f"/instances/{iid}").json()
    assert got["status"] == "completed"
    assert not got["open_tasks"]


def test_non_assignee_cannot_operate_todo(client, req_id):
    create_published(client, "auth", simple_chain_def(mode="any", assignees=("a1",)))
    inst = start(client, "auth")
    r = decide(client, instance_id=inst["id"], version=inst["version"], actor="mallory",
               decision="approve", request_id=req_id())
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "not_assignee"


def test_concurrent_approvals_any_sign_only_one_advances(client, req_id):
    create_published(client, "race-any", simple_chain_def(mode="any", assignees=("a1", "a2")))
    inst = start(client, "race-any")
    iid, ver = inst["id"], inst["version"]
    barrier = threading.Barrier(2)
    results: list[int] = []
    lock = threading.Lock()

    def worker(actor):
        from app.db import SessionLocal
        from app.schemas import DecisionIn
        from app.engine import decide as engine_decide

        barrier.wait()
        db = SessionLocal()
        try:
            body = DecisionIn(
                request_id=f"race-{actor}-{iid}",
                instance_id=iid,
                expected_version=ver,
                actor=actor,
                decision="approve",
            )
            _, created = engine_decide(db, body)
            with lock:
                results.append(200 if created else 200)
        except Exception as exc:  # noqa: BLE001
            with lock:
                results.append(getattr(exc, "status_code", 500))
        finally:
            db.close()

    t1 = threading.Thread(target=worker, args=("a1",))
    t2 = threading.Thread(target=worker, args=("a2",))
    t1.start(); t2.start(); t1.join(); t2.join()

    # One wins and advances; the loser either got its todo closed (409) or —
    # if it processed first — won. In no case may both transitions land.
    assert sorted(results) in ([200, 200], [200, 409]), results
    winners = results.count(200)
    assert winners == 1
    got = client.get(f"/instances/{iid}").json()
    assert got["status"] == "completed"
    # exactly one approval decision recorded
    hist = client.get(f"/instances/{iid}/history").json()["history"]
    decisions = [h for h in hist if h["action"] == "task_decision"]
    assert len(decisions) == 1


def test_concurrent_all_sign_completes_once(client, req_id):
    """Two last-required approvals racing (one approve, one reject) ->
    exactly one terminal outcome."""
    create_published(client, "race-all", simple_chain_def(mode="all", assignees=("a1", "a2")))
    inst = start(client, "race-all")
    iid, ver = inst["id"], inst["version"]
    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()

    def worker(actor, decision):
        from app.db import SessionLocal
        from app.schemas import DecisionIn
        from app.engine import decide as engine_decide

        barrier.wait()
        db = SessionLocal()
        try:
            body = DecisionIn(
                request_id=f"race-{decision}-{actor}-{iid}",
                instance_id=iid,
                expected_version=ver,
                actor=actor,
                decision=decision,
                comment="c",
            )
            res, _ = engine_decide(db, body)
            with lock:
                outcomes.append(res["result"])
        except Exception as exc:  # noqa: BLE001
            with lock:
                outcomes.append(f"err:{getattr(exc, 'code', 'unknown')}")
        finally:
            db.close()

    t1 = threading.Thread(target=worker, args=("a1", "approve"))
    t2 = threading.Thread(target=worker, args=("a2", "reject"))
    t1.start(); t2.start(); t1.join(); t2.join()

    got = client.get(f"/instances/{iid}").json()
    statuses = {"completed", "rejected"}
    assert got["status"] in statuses
    terminal = [o for o in outcomes if o in ("approved_advanced", "rejected")]
    assert len(terminal) == 1, outcomes
    # no pending todos survive
    assert not got["open_tasks"]
