"""Withdrawal rules, assignee authorization and current-position reads."""
import pytest

from tests.factories import (
    approve,
    linear_template,
    publish,
    reject,
    req_id,
    start,
)


@pytest.mark.asyncio
async def test_only_submitter_may_withdraw(client):
    await publish(client, "wd-auth", linear_template(("u1",), "any"))
    inst = await start(client, "wd-auth", submitter="alice")

    r = await client.post(
        f"/instances/{inst['id']}/withdraw",
        json={},
        headers={"X-User-Id": "mallory"},
    )
    assert r.status_code == 403

    r = await client.post(
        f"/instances/{inst['id']}/withdraw",
        json={"comment": "mistake"},
        headers={"X-User-Id": "alice"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "withdrawn"


@pytest.mark.asyncio
async def test_withdraw_closes_pending_tasks_and_blocks_later_decisions(client):
    await publish(client, "wd-close", linear_template(("u1",), "any"))
    inst = await start(client, "wd-close", submitter="alice")
    vid = inst["template_version"]

    await client.post(
        f"/instances/{inst['id']}/withdraw",
        json={},
        headers={"X-User-Id": "alice"},
    )
    late = await approve(client, inst["id"], "u1", vid)
    assert late.status_code == 409

    detail = (await client.get(f"/instances/{inst['id']}")).json()
    assert detail["pending_tasks"] == []
    assert detail["history"][-1]["event_type"] == "withdrawn"


@pytest.mark.asyncio
async def test_cannot_withdraw_after_end(client):
    await publish(client, "wd-terminal", linear_template(("u1",), "any"))
    inst = await start(client, "wd-terminal", submitter="alice")
    vid = inst["template_version"]
    assert (await approve(client, inst["id"], "u1", vid)).status_code == 200

    r = await client.post(
        f"/instances/{inst['id']}/withdraw",
        json={},
        headers={"X-User-Id": "alice"},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_non_assignee_cannot_decide_even_with_known_instance_id(client):
    code = "assignee"
    await publish(client, code, linear_template(("u1", "u2"), "all"))
    inst = await start(client, code)
    vid = inst["template_version"]

    # mallory knows the instance id but has no task on it
    r = await approve(client, inst["id"], "mallory", vid)
    assert r.status_code == 403
    assert r.json()["code"] == "forbidden"

    # an outsider who learns/guesses another instance id cannot act on its
    # tasks: authorization is checked against that instance's own todos, so
    # substituting an instance id grants nothing
    other = await start(client, code)
    r = await client.post(
        f"/instances/{other['id']}/decision",
        json={"action": "approve", "expected_version": vid},
        headers={"X-Request-Id": req_id(), "X-User-Id": "mallory"},
    )
    assert r.status_code == 403

    # and the first instance is unaffected by the rejected attempt
    detail = (await client.get(f"/instances/{inst['id']}")).json()
    assert detail["status"] == "running"
    assert {t["assignee"] for t in detail["pending_tasks"]} == {"u1", "u2"}


@pytest.mark.asyncio
async def test_current_position_and_reject_reason_are_visible(client):
    await publish(client, "position", linear_template(("u1",), "any"))
    inst = await start(client, "position")
    vid = inst["template_version"]

    detail = (await client.get(f"/instances/{inst['id']}")).json()
    assert detail["status"] == "running"
    assert detail["current_node_id"] == "approve"
    assert len(detail["pending_tasks"]) == 1
    assert detail["reject_reason"] is None

    r = await reject(client, inst["id"], "u1", vid, comment="budget exceeded")
    assert r.status_code == 200

    detail = (await client.get(f"/instances/{inst['id']}")).json()
    assert detail["status"] == "rejected"
    assert detail["reject_reason"] == "budget exceeded"
    types = [e["event_type"] for e in detail["history"]]
    assert "started" in types and "enter_node" in types and "rejected" in types


@pytest.mark.asyncio
async def test_missing_request_id_is_rejected(client):
    await publish(client, "no-rid", linear_template(("u1",), "any"))
    inst = await start(client, "no-rid")
    r = await client.post(
        f"/instances/{inst['id']}/decision",
        json={"action": "approve", "expected_version": 1},
        headers={"X-User-Id": "u1"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "missing_request_id"


@pytest.mark.asyncio
async def test_missing_user_header_rejected(client):
    await publish(client, "no-user", linear_template(("u1",), "any"))
    inst = await start(client, "no-user")
    r = await client.post(
        f"/instances/{inst['id']}/decision",
        json={"action": "approve", "expected_version": 1},
        headers={"X-Request-Id": req_id()},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_unknown_instance_404(client):
    r = await client.get("/instances/99999")
    assert r.status_code == 404
