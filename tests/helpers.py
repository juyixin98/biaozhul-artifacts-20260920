"""Shared helpers for building definitions and driving the API."""
from __future__ import annotations


def simple_chain_def(mode="all", assignees=("a1", "a2"), *, timeout=None, escalate_to=None,
                     start_id="s", approval_id="ap", ok_id="ok"):
    approval = {
        "id": approval_id,
        "type": "approval",
        "name": "审批",
        "mode": mode,
        "assignees": list(assignees),
    }
    if timeout is not None:
        approval["timeout_seconds"] = timeout
        approval["escalate_to"] = escalate_to or []
    return {
        "nodes": [
            {"id": start_id, "type": "start", "name": "开始"},
            approval,
            {"id": ok_id, "type": "end", "name": "通过", "terminal": "approved"},
        ],
        "edges": [
            {"source": start_id, "target": approval_id},
            {"source": approval_id, "target": ok_id},
        ],
    }


def condition_def():
    return {
        "nodes": [
            {"id": "s", "type": "start", "name": "开始"},
            {"id": "c", "type": "condition", "name": "判断"},
            {"id": "big", "type": "approval", "name": "大额审批", "mode": "any",
             "assignees": ["boss"]},
            {"id": "small", "type": "approval", "name": "小额审批", "mode": "any",
             "assignees": ["clerk"]},
            {"id": "ok", "type": "end", "name": "通过", "terminal": "approved"},
        ],
        "edges": [
            {"source": "s", "target": "c"},
            {"source": "c", "target": "big", "expression": "amount >= 10000"},
            {"source": "c", "target": "small"},  # default
            {"source": "big", "target": "ok"},
            {"source": "small", "target": "ok"},
        ],
    }


def create_published(client, key, definition, name="t"):
    r = client.post("/templates", json={"key": key, "name": name, "definition": definition})
    assert r.status_code == 201, r.text
    r = client.post(f"/templates/{key}/versions/1/publish")
    assert r.status_code == 200, r.text


def start(client, key, *, submitter="u1", version=None, payload=None):
    body = {"template_key": key, "submitter": submitter}
    if version is not None:
        body["version"] = version
    if payload is not None:
        body["payload"] = payload
    r = client.post("/instances", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def decide(client, *, instance_id, version, actor, decision, request_id, comment=None):
    body = {
        "request_id": request_id,
        "instance_id": str(instance_id),
        "expected_version": version,
        "actor": actor,
        "decision": decision,
    }
    if comment is not None:
        body["comment"] = comment
    return client.post("/decisions", json=body)


def withdraw(client, *, instance_id, submitter, request_id, expected_version=None):
    body = {
        "request_id": request_id,
        "instance_id": str(instance_id),
        "submitter": submitter,
    }
    if expected_version is not None:
        body["expected_version"] = expected_version
    return client.post("/withdrawals", json=body)
