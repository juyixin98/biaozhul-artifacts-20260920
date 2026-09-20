"""全签/任签 semantics under concurrency.

Concurrent requests hit the real server concurrently (httpx on an ASGI
transport); PostgreSQL row locks are what serialise them.
"""
import asyncio

import pytest

from tests.factories import (
    approve,
    linear_template,
    publish,
    reject,
    start,
)


@pytest.mark.asyncio
async def test_all_sign_requires_every_approver(client):
    code = "all-sign"
    await publish(client, code, linear_template(("a1", "a2", "a3"), "all"))
    inst = await start(client, code)
    vid = inst["template_version"]

    r1 = await approve(client, inst["id"], "a1", vid)
    assert r1.status_code == 200
    assert r1.json()["status"] == "running"
    assert {t["assignee"] for t in r1.json()["pending_tasks"]} == {"a2", "a3"}

    r2 = await approve(client, inst["id"], "a2", vid)
    assert r2.json()["status"] == "running"
    assert {t["assignee"] for t in r2.json()["pending_tasks"]} == {"a3"}

    r3 = await approve(client, inst["id"], "a3", vid)
    assert r3.status_code == 200
    assert r3.json()["status"] == "approved"
    assert r3.json()["current_node_id"] == "end"


@pytest.mark.asyncio
async def test_all_sign_rejection_terminates_and_closes_other_tasks(client):
    code = "all-sign-reject"
    await publish(client, code, linear_template(("a1", "a2"), "all"))
    inst = await start(client, code)
    vid = inst["template_version"]

    r = await reject(client, inst["id"], "a2", vid, comment="not allowed")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rejected"
    assert body["reject_reason"] == "not allowed"

    # no pending work remains; a1's task was closed, not silently approved
    detail = (await client.get(f"/instances/{inst['id']}")).json()
    assert detail["pending_tasks"] == []

    # a1 can no longer act: instance no longer running
    late = await approve(client, inst["id"], "a1", vid)
    assert late.status_code == 409


@pytest.mark.asyncio
async def test_any_sign_first_approval_wins_and_closes_others(client):
    code = "any-sign"
    await publish(client, code, linear_template(("a1", "a2"), "any"))
    inst = await start(client, code)
    vid = inst["template_version"]

    r = await approve(client, inst["id"], "a2", vid)
    assert r.status_code == 200
    assert r.json()["status"] == "approved"

    detail = (await client.get(f"/instances/{inst['id']}")).json()
    tasks = detail["history"]
    closed = [e for e in tasks if e["event_type"] == "task_closed"]
    assert len(closed) == 1
    assert closed[0]["detail"]["assignee"] == "a1"

    late = await approve(client, inst["id"], "a1", vid)
    assert late.status_code == 409


@pytest.mark.asyncio
async def test_concurrent_all_sign_approvals_each_count_once(client):
    code = "countersign-race"
    await publish(client, code, linear_template(("c1", "c2"), "all"))
    inst = await start(client, code)
    vid = inst["template_version"]
    iid = inst["id"]

    async def do(actor):
        return await approve(client, iid, actor, vid)

    results = await asyncio.gather(
        do("c1"), do("c2"), do("c1"), do("c2")
    )
    statuses = [r.status_code for r in results]
    # exactly two approvals land; the duplicates find no pending task -> 409
    assert sorted(statuses) == [200, 200, 409, 409]

    detail = (await client.get(f"/instances/{iid}")).json()
    assert detail["status"] == "approved"
    approved_events = [
        e for e in detail["history"] if e["event_type"] == "approved"
    ]
    assert len(approved_events) == 2
    actors = sorted(e["actor"] for e in approved_events)
    assert actors == ["c1", "c2"]


@pytest.mark.asyncio
async def test_concurrent_any_sign_only_one_approval(client):
    code = "anysign-race"
    await publish(client, code, linear_template(("c1", "c2", "c3"), "any"))
    inst = await start(client, code)
    vid = inst["template_version"]
    iid = inst["id"]

    results = await asyncio.gather(
        *[approve(client, iid, f"c{i}", vid) for i in (1, 2, 3)]
    )
    ok = [r for r in results if r.status_code == 200]
    conflicts = [r for r in results if r.status_code == 409]
    assert len(ok) == 1
    assert len(conflicts) == 2

    detail = (await client.get(f"/instances/{iid}")).json()
    assert detail["status"] == "approved"
    approved_events = [
        e for e in detail["history"] if e["event_type"] == "approved"
    ]
    assert len(approved_events) == 1
    closed = [e for e in detail["history"]
              if e["event_type"] == "task_closed"]
    assert len(closed) == 2


@pytest.mark.asyncio
async def test_concurrent_approve_and_reject_yields_single_transition(client):
    code = "approve-reject-race"
    await publish(client, code, linear_template(("p1", "p2"), "any"))
    inst = await start(client, code)
    vid = inst["template_version"]
    iid = inst["id"]

    results = await asyncio.gather(
        approve(client, iid, "p1", vid),
        reject(client, iid, "p2", vid, comment="no"),
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 409]

    detail = (await client.get(f"/instances/{iid}")).json()
    assert detail["status"] in ("approved", "rejected")
    # whichever lost never recorded its terminal event
    terminal = [e for e in detail["history"]
                if e["event_type"] in ("approved", "rejected")]
    assert len(terminal) == 1
