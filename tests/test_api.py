"""API 端到端测试（httpx ASGI transport，无需真实监听端口）。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


SCHED = {
    "priority_policy": "RM",
    "tasks": [
        {"id": "tau1", "C": 1, "T": 4, "D": 4, "B": 0},
        {"id": "tau2", "C": 2, "T": 6, "D": 6, "B": 1},
        {"id": "tau3", "C": 1, "T": 8, "D": 8, "B": 1},
    ],
}
UNSCHED_U_LT_1 = {
    "priority_policy": "RM",
    "tasks": [
        {"id": "tau1", "C": 1, "T": 4, "D": 4},
        {"id": "tau2", "C": 2, "T": 6, "D": 6},
        {"id": "tau3", "C": 3, "T": 8, "D": 8},
    ],
}
OVERLOAD = {
    "priority_policy": "RM",
    "tasks": [
        {"id": "fast", "C": 2, "T": 3, "D": 3},
        {"id": "slow", "C": 3, "T": 4, "D": 4},
    ],
}


def test_root_declares_model_and_assumptions():
    info = client.get("/").json()
    assert "Liu & Layland" in info["model"]
    assert any("单核" in a for a in info["assumptions"])
    assert any("独立" in a for a in info["assumptions"])
    assert info["priority_policy"].startswith("RM")
    assert "输入顺序" in info["tie_break_rule"]


def test_health():
    h = client.get("/api/health").json()
    assert h["status"] == "ok"
    assert h["hmac_key_source"] in {"env:RTA_HMAC_KEY", "ephemeral:secrets.token_bytes(32)"}


def test_analyze_schedulable():
    resp = client.post("/api/analyze", json=SCHED)
    assert resp.status_code == 200
    body = resp.json()
    assert body["schedulable"] is True
    assert body["simulation"]["matches_rta"] is True
    assert body["simulation"]["mismatch"] is None
    outcomes = {t["task_id"]: t for t in body["tasks"]}
    assert all(t["outcome"] == "converged" for t in body["tasks"])
    assert outcomes["tau3"]["final_rt"] == 6
    # 干扰来源、迭代过程存在
    tau3_steps = outcomes["tau3"]["iterations"]
    assert {i["task_id"] for i in tau3_steps[-1]["interferences"]} == {"tau1", "tau2"}


def test_analyze_unschedulable_low_utilization():
    body = client.post("/api/analyze", json=UNSCHED_U_LT_1).json()
    assert body["schedulable"] is False
    # 利用率 < 1 仍然不可调度：证明不把低利用率当充分条件
    assert body["utilization"]["total_utilization_float"] < 1.0
    tau3 = next(t for t in body["tasks"] if t["task_id"] == "tau3")
    assert tau3["outcome"] == "deadline_miss"
    assert tau3["deadline_miss"] >= 1
    assert body["simulation"]["matches_rta"] is True


def test_analyze_overload():
    body = client.post("/api/analyze", json=OVERLOAD).json()
    assert body["schedulable"] is False
    slow = next(t for t in body["tasks"] if t["task_id"] == "slow")
    assert slow["outcome"] == "deadline_miss"
    assert body["simulation"]["matches_rta"] is True


def test_reject_deadline_gt_period():
    resp = client.post(
        "/api/analyze",
        json={"tasks": [{"id": "a", "C": 3, "T": 8, "D": 10}]},
    )
    assert resp.status_code == 422
    assert "D_i <= T_i" in resp.text


def test_reject_unknown_policy():
    resp = client.post(
        "/api/analyze",
        json={
            "priority_policy": "EDF",
            "tasks": [{"id": "a", "C": 1, "T": 4, "D": 4}],
        },
    )
    assert resp.status_code == 422


def test_response_carries_real_crypto_and_verifies():
    from app.integrity import verify

    body = client.post("/api/analyze", json=SCHED).json()
    assert len(body["sha256"]) == 64
    assert len(body["hmac_sha256"]) == 64
    assert verify(body, body["hmac_sha256"]) is True
    # 篡改任意计算结果后验签必须失败
    tampered = dict(body)
    tampered["schedulable"] = False
    assert verify(tampered, body["hmac_sha256"]) is False


def test_iteration_steps_serialized():
    body = client.post("/api/analyze", json=SCHED).json()
    tau2 = next(t for t in body["tasks"] if t["task_id"] == "tau2")
    seqs = [(s["step"], s["r_prev"], s["r_next"]) for s in tau2["iterations"]]
    assert seqs[0] == (0, 0, 3)
    assert seqs[1][:2] == (1, 3)
    assert seqs[-1][2] == 4  # 不动点
