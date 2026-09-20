"""Idempotency: duplicate request_id replays the original and writes once."""
from __future__ import annotations

from tests.helpers import create_published, decide, simple_chain_def, start, withdraw


def test_duplicate_decision_returns_same_result(client, req_id):
    create_published(client, "id1", simple_chain_def(mode="any", assignees=("a1", "a2")))
    inst = start(client, "id1")
    iid, ver = inst["id"], inst["version"]
    rid = req_id()

    r1 = decide(client, instance_id=iid, version=ver, actor="a1",
                decision="approve", request_id=rid)
    assert r1.status_code == 200
    assert r1.json()["replayed"] is False

    r2 = decide(client, instance_id=iid, version=ver, actor="a1",
                decision="approve", request_id=rid)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["result"] == r1.json()["result"]

    hist = client.get(f"/instances/{iid}/history").json()["history"]
    assert len([h for h in hist if h["action"] == "task_decision"]) == 1


def test_same_request_id_different_payload_is_conflict(client, req_id):
    create_published(client, "id2", simple_chain_def(mode="all", assignees=("a1", "a2")))
    inst = start(client, "id2")
    iid, ver = inst["id"], inst["version"]
    rid = req_id()

    assert decide(client, instance_id=iid, version=ver, actor="a1",
                  decision="approve", request_id=rid).status_code == 200

    # same request_id, different actor
    r = decide(client, instance_id=iid, version=ver, actor="a2",
               decision="approve", request_id=rid)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_conflict"

    # same request_id, flipped decision
    r = decide(client, instance_id=iid, version=ver, actor="a1",
               decision="reject", request_id=rid)
    assert r.status_code == 409

    # conflicting repeat wrote no history and a1's approval is still in place
    hist = client.get(f"/instances/{iid}/history").json()["history"]
    assert len([h for h in hist if h["action"] == "task_decision"]) == 1


def test_duplicate_withdraw_replays(client, req_id):
    create_published(client, "id3", simple_chain_def(mode="any", assignees=("a1",)))
    inst = start(client, "id3", submitter="owner")
    rid = req_id()

    r1 = withdraw(client, instance_id=inst["id"], submitter="owner", request_id=rid)
    assert r1.status_code == 200
    assert r1.json()["result"] == "withdrawn"
    r2 = withdraw(client, instance_id=inst["id"], submitter="owner", request_id=rid)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True

    hist = client.get(f"/instances/{iid}/history") if False else client.get(
        f"/instances/{inst['id']}/history"
    ).json()["history"]
    assert len([h for h in hist if h["action"] == "instance_withdrawn"]) == 1


def test_only_submitter_can_withdraw(client, req_id):
    create_published(client, "id4", simple_chain_def(mode="any", assignees=("a1",)))
    inst = start(client, "id4", submitter="owner")
    r = withdraw(client, instance_id=inst["id"], submitter="intruder", request_id=req_id())
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "not_submitter"


def test_cannot_withdraw_finished_instance(client, req_id):
    create_published(client, "id5", simple_chain_def(mode="any", assignees=("a1",)))
    inst = start(client, "id5", submitter="owner")
    decide(client, instance_id=inst["id"], version=inst["version"], actor="a1",
           decision="approve", request_id=req_id())
    r = withdraw(client, instance_id=inst["id"], submitter="owner", request_id=req_id())
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "instance_finished"


def test_replayed_request_does_not_reopen_closed_flow(client, req_id):
    create_published(client, "id6", simple_chain_def(mode="all", assignees=("a1", "a2")))
    inst = start(client, "id6")
    iid, ver = inst["id"], inst["version"]

    # a1 approves, waits
    rid1 = req_id()
    decide(client, instance_id=iid, version=ver, actor="a1",
           decision="approve", request_id=rid1)
    # a2 rejects -> rejected
    decide(client, instance_id=iid, version=ver, actor="a2",
           decision="reject", request_id=req_id())
    # replay a1's old approve returns the stored waiting result, flow unchanged
    replay = decide(client, instance_id=iid, version=ver, actor="a1",
                    decision="approve", request_id=rid1)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["result"] == "waiting_for_others"
    assert client.get(f"/instances/{iid}").json()["status"] == "rejected"
