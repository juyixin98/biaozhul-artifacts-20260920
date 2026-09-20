"""端到端 HTTP 接口测试（覆盖主要路径）。"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api import app
from app.db import SessionLocal
from app.models import Escalation


@pytest.fixture()
def client(engine_):
    with TestClient(app) as c:
        yield c


def _rid():
    return f"req-{uuid.uuid4().hex}"


DEMO_DEFINITION = {
    "start_node": "start",
    "nodes": [
        {"id": "start", "type": "start", "next": "mgr"},
        {"id": "mgr", "type": "approval", "mode": "all",
         "assignees": ["alice", "bob"], "next": "check"},
        {"id": "check", "type": "condition",
         "branches": [{"when": "amount >= 10000", "next": "end"}],
         "default": "end2"},
        {"id": "end", "type": "end"},
        {"id": "end2", "type": "end"},
    ],
}


def test_full_flow_via_http(client):
    # 创建 + 发布
    r = client.post("/templates", json={
        "key": "exp", "name": "报销", "definition": DEMO_DEFINITION})
    assert r.status_code == 201, r.text
    r = client.post("/templates/exp/versions/1/publish")
    assert r.status_code == 200, r.text

    # 非法定义创建模板：422
    bad = {"start_node": "start", "nodes": [
        {"id": "start", "type": "start", "next": "missing"},
        {"id": "start", "type": "end"},
    ]}
    r = client.post("/templates", json={"key": "bad", "name": "x", "definition": bad})
    assert r.status_code == 422

    # 启动实例
    req = _rid()
    r = client.post("/instances", json={
        "template_key": "exp", "submitter": "zhang",
        "context": {"amount": 100}, "request_id": req})
    assert r.status_code == 201, r.text
    inst = r.json()
    iid = inst["instance_id"]
    assert inst["current_node_id"] == "mgr"

    # 幂等重放
    r2 = client.post("/instances", json={
        "template_key": "exp", "submitter": "zhang",
        "context": {"amount": 100}, "request_id": req})
    assert r2.status_code == 200
    assert r2.json()["instance_id"] == iid

    # 非审批人操作：403
    t = inst["pending_tasks"][0]
    r = client.post(f"/instances/{iid}/tasks/{t['task_id']}/decision", json={
        "decision": "approve", "actor": "mallory",
        "expected_version": 1, "request_id": _rid()})
    assert r.status_code == 403

    # 替换实例 ID：404
    r = client.post(f"/instances/{uuid.uuid4()}/tasks/{t['task_id']}/decision", json={
        "decision": "approve", "actor": "alice",
        "expected_version": 1, "request_id": _rid()})
    assert r.status_code == 404

    # 正常会签
    for task in inst["pending_tasks"]:
        r = client.post(f"/instances/{iid}/tasks/{task['task_id']}/decision", json={
            "decision": "approve", "actor": task["assignee"],
            "expected_version": 1, "request_id": _rid()})
        assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"

    # 历史
    r = client.get(f"/instances/{iid}/history")
    assert r.status_code == 200
    types = [e["event_type"] for e in r.json()]
    assert "start" in types and "complete" in types and "node_approve" in types


def test_reject_reason_returned(client):
    client.post("/templates", json={
        "key": "exp2", "name": "报销2", "definition": DEMO_DEFINITION})
    client.post("/templates/exp2/versions/1/publish")
    r = client.post("/instances", json={
        "template_key": "exp2", "submitter": "zhang",
        "context": {}, "request_id": _rid()})
    inst = r.json()
    t = inst["pending_tasks"][0]
    r = client.post(f"/instances/{inst['instance_id']}/tasks/{t['task_id']}/decision",
                    json={"decision": "reject", "actor": t["assignee"],
                          "expected_version": 1, "request_id": _rid(),
                          "comment": "预算不足"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rejected"
    assert body["reject_reason"] == "预算不足"


def test_withdraw_via_http(client):
    client.post("/templates", json={
        "key": "exp3", "name": "报销3", "definition": DEMO_DEFINITION})
    client.post("/templates/exp3/versions/1/publish")
    r = client.post("/instances", json={
        "template_key": "exp3", "submitter": "zhang",
        "context": {}, "request_id": _rid()})
    iid = r.json()["instance_id"]
    r = client.post(f"/instances/{iid}/withdraw",
                    json={"actor": "zhang", "request_id": _rid()})
    assert r.status_code == 200
    assert r.json()["status"] == "withdrawn"
