"""Shared helpers for building templates via the API."""
import uuid


def linear_template(approvers=("u1",), strategy="any", approver_next="end"):
    return {
        "nodes": [
            {"id": "start", "type": "start", "next_node": "approve"},
            {"id": "approve", "type": "approval", "strategy": strategy,
             "approvers": list(approvers), "next_node": approver_next},
            {"id": "end", "type": "end"},
        ]
    }


def branch_template():
    return {
        "nodes": [
            {"id": "start", "type": "start", "next_node": "cond"},
            {"id": "cond", "type": "condition",
             "branches": [
                 {"expression": "amount >= 1000", "next_node": "big"},
             ],
             "default": "small"},
            {"id": "big", "type": "approval", "strategy": "any",
             "approvers": ["bigboss"], "next_node": "end"},
            {"id": "small", "type": "approval", "strategy": "any",
             "approvers": ["clerk"], "next_node": "end"},
            {"id": "end", "type": "end"},
        ]
    }


def req_id():
    return uuid.uuid4().hex


async def publish(client, code, definition, name="tpl"):
    resp = await client.post(
        f"/templates/{code}/versions",
        json={"name": name, "definition": definition},
        headers={"X-Request-Id": req_id()},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def start(client, code, variables=None, business_key=None,
                version=None, submitter="alice"):
    resp = await client.post(
        "/instances",
        json={
            "template_code": code,
            "template_version": version,
            "business_key": business_key or uuid.uuid4().hex,
            "variables": variables or {},
            "submitter": submitter,
        },
        headers={"X-Request-Id": req_id()},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def decide(client, instance_id, actor, action, version, comment=None,
                 request=None):
    resp = await client.post(
        f"/instances/{instance_id}/decision",
        json={"action": action, "expected_version": version,
              "comment": comment},
        headers={"X-Request-Id": request or req_id(),
                 "X-User-Id": actor},
    )
    return resp


async def approve(client, instance_id, actor, expected_version, **kw):
    return await decide(client, instance_id, actor, "approve", expected_version, **kw)


async def reject(client, instance_id, actor, expected_version, **kw):
    return await decide(client, instance_id, actor, "reject", expected_version, **kw)
