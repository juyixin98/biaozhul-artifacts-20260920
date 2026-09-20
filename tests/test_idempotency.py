"""Idempotency: same request id -> same result; conflicting reuse -> 409."""
import asyncio

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
async def test_duplicate_approval_returns_original_result(client):
    code = "idem-approve"
    await publish(client, code, linear_template(("u1",), "any"))
    inst = await start(client, code)
    vid = inst["template_version"]

    rid = req_id()
    r1 = await approve(client, inst["id"], "u1", vid, request=rid,
                       comment="first")
    r2 = await approve(client, inst["id"], "u1", vid, request=rid,
                       comment="replayed")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()

    detail = (await client.get(f"/instances/{inst['id']}")).json()
    approvals = [e for e in detail["history"]
                 if e["event_type"] == "approved"]
    assert len(approvals) == 1
    assert approvals[0]["detail"]["comment"] == "first"


@pytest.mark.asyncio
async def test_same_request_id_different_action_conflicts(client):
    code = "idem-conflict-action"
    await publish(client, code, linear_template(("u1",), "any"))
    inst = await start(client, code)
    vid = inst["template_version"]

    rid = req_id()
    r1 = await approve(client, inst["id"], "u1", vid, request=rid)
    assert r1.status_code == 200

    r2 = await reject(client, inst["id"], "u1", vid, request=rid,
                      comment="trying reject with same id")
    assert r2.status_code == 409
    assert r2.json()["code"] == "request_id_conflict"


@pytest.mark.asyncio
async def test_same_request_id_different_instance_conflicts(client):
    code = "idem-conflict-instance"
    await publish(client, code, linear_template(("u1",), "any"))
    inst1 = await start(client, code)
    inst2 = await start(client, code)
    vid = inst1["template_version"]

    rid = req_id()
    r1 = await approve(client, inst1["id"], "u1", vid, request=rid)
    assert r1.status_code == 200

    r2 = await approve(client, inst2["id"], "u1", vid, request=rid)
    assert r2.status_code == 409
    assert r2.json()["code"] == "request_id_conflict"


@pytest.mark.asyncio
async def test_concurrent_same_request_id_serialises_to_one_effect(client):
    code = "idem-concurrent"
    await publish(client, code, linear_template(("u1", "u2"), "any"))
    inst = await start(client, code)
    vid = inst["template_version"]

    rid = req_id()
    # same actor reusing the same request id concurrently
    results = await asyncio.gather(
        approve(client, inst["id"], "u1", vid, request=rid),
        approve(client, inst["id"], "u1", vid, request=rid),
    )
    assert [r.status_code for r in results] == [200, 200]
    body1, body2 = (r.json() for r in results)
    assert body1 == body2

    detail = (await client.get(f"/instances/{inst['id']}")).json()
    assert len([e for e in detail["history"]
                if e["event_type"] == "approved"]) == 1


@pytest.mark.asyncio
async def test_start_request_id_replay_returns_same_instance(client):
    code = "idem-start"
    await publish(client, code, linear_template(("u1",), "any"))
    rid = req_id()

    payload = {
        "template_code": code,
        "business_key": "BK-1",
        "variables": {},
        "submitter": "alice",
    }
    r1 = await client.post(
        "/instances", json=payload,
        headers={"X-Request-Id": rid, "X-User-Id": "alice"},
    )
    r2 = await client.post(
        "/instances", json={**payload, "business_key": "BK-2"},
        headers={"X-Request-Id": rid, "X-User-Id": "alice"},
    )
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]


@pytest.mark.asyncio
async def test_withdraw_request_id_replay(client):
    code = "idem-withdraw"
    await publish(client, code, linear_template(("u1",), "any"))
    inst = await start(client, code, submitter="alice")
    rid = req_id()
    headers = {"X-Request-Id": rid, "X-User-Id": "alice"}
    r1 = await client.post(
        f"/instances/{inst['id']}/withdraw",
        json={"comment": "changed my mind"}, headers=headers,
    )
    r2 = await client.post(
        f"/instances/{inst['id']}/withdraw",
        json={"comment": "changed my mind again"}, headers=headers,
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()


@pytest.mark.asyncio
async def test_same_request_id_different_template_code_conflicts(client):
    await publish(client, "code-a", linear_template(("u1",), "any"))
    await publish(client, "code-b", linear_template(("u1",), "any"))
    rid = req_id()
    payload_a = {
        "template_code": "code-a", "business_key": "BK-A",
        "variables": {}, "submitter": "alice",
    }
    payload_b = {**payload_a, "template_code": "code-b", "business_key": "BK-B"}
    r1 = await client.post("/instances", json=payload_a,
                           headers={"X-Request-Id": rid, "X-User-Id": "alice"})
    r2 = await client.post("/instances", json=payload_b,
                           headers={"X-Request-Id": rid, "X-User-Id": "alice"})
    assert r1.status_code == 201
    assert r2.status_code == 409
    assert r2.json()["code"] == "request_id_conflict"
