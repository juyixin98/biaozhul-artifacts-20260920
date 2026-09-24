"""FastAPI HTTP 接口测试（FastAPI TestClient，基于 httpx）。"""

import math

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_root_and_health():
    r = client.get("/")
    assert r.status_code == 200
    assert "endpoints" in r.json()
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_fk_endpoint():
    r = client.post("/fk", json={"q1": 0.0, "q2": math.pi / 2})
    assert r.status_code == 200
    data = r.json()
    assert data["end_effector"] == pytest.approx([1.0, 1.0], abs=1e-12)
    assert data["elbow"] == pytest.approx([1.0, 0.0], abs=1e-12)


def test_ik_endpoint_both_branches():
    r = client.post("/ik", json={"target": {"x": 1.0, "y": 1.0}})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["success"] is True
    names = {b["name"] for b in data["branches"]}
    assert names == {"elbow_up", "elbow_down"}
    assert data["position_error"] < 1e-12
    assert data["workspace"]["r_max"] == 2.0
    assert data["workspace"]["r_min"] == 0.0


def test_ik_full_extension():
    x, y = math.sqrt(2), math.sqrt(2)  # r=2, angle pi/4
    r = client.post("/ik", json={"target": {"x": x, "y": y}})
    data = r.json()
    assert data["status"] == "full_extension"
    assert data["singular"] is True
    assert data["joints"][1] == pytest.approx(0.0, abs=1e-12)


def test_ik_full_fold():
    r = client.post("/ik", json={"target": {"x": 0.0, "y": 0.0}})
    data = r.json()
    assert data["status"] == "full_fold"
    assert data["singular"] is True


def test_ik_unreachable_no_clamping():
    r = client.post("/ik", json={"target": {"x": 3.0, "y": 0.0}})
    data = r.json()
    assert r.status_code == 200
    assert data["status"] == "unreachable"
    assert data["success"] is False
    assert data["joints"] is None
    assert data["boundary_gap"] == pytest.approx(1.0, abs=1e-9)


def test_ik_preference_and_previous_joints():
    r = client.post(
        "/ik",
        json={
            "target": {"x": 1.0, "y": 1.0},
            "previous_joints": [0.0, math.pi / 2],
        },
    )
    assert r.json()["chosen"] == "elbow_down"
    r = client.post(
        "/ik",
        json={"target": {"x": 1.0, "y": 1.0}, "preference": "elbow_up"},
    )
    assert r.json()["chosen"] == "elbow_up"


def test_ik_limit_violation_via_params():
    r = client.post(
        "/ik",
        json={
            "target": {"x": 1.0, "y": 1.0},
            "params": {"theta2_min": 0.3, "theta2_max": 0.5},
        },
    )
    data = r.json()
    assert data["status"] == "limit_violation"
    assert data["joints"] is None
    assert all(not b["feasible"] for b in data["branches"])


def test_ik_path_endpoint_continuous():
    # 内建圆路径参数 center=(0.5,0), r=0.8
    targets = [
        {"x": 0.5 + 0.8 * math.cos(2 * math.pi * k / 20),
         "y": 0.8 * math.sin(2 * math.pi * k / 20)}
        for k in range(20)
    ]
    r = client.post("/ik/path", json={"targets": targets})
    data = r.json()
    assert r.status_code == 200
    assert data["success"] is True
    assert data["failed_count"] == 0
    assert data["max_position_error"] < 1e-10
    # 圆路径经过近原点 r=0.3（近完全折叠奇异），关节空间天然放大
    # （最大步长约 0.79 rad），但连续选支不翻转：
    assert data["flip_count"] == 0
    assert data["max_joint_step"] < 1.0


def test_ik_path_with_unreachable_gap():
    targets = [
        {"x": 1.0, "y": 1.0},
        {"x": 2.8, "y": 0.0},
        {"x": 3.0, "y": 0.0},
        {"x": 1.0, "y": 1.0},
    ]
    r = client.post("/ik/path", json={"targets": targets})
    data = r.json()
    assert data["success"] is False
    assert data["failed_count"] == 2
    assert data["points"][1]["status"] == "unreachable"
    assert data["points"][2]["status"] == "unreachable"
    assert data["points"][1]["joints"] is None


def test_invalid_payloads():
    # 非有限数
    r = client.post("/ik", json={"target": {"x": "nan", "y": 0.0}})
    assert r.status_code == 422
    # 非法连杆长度
    r = client.post("/fk", json={"q1": 0, "q2": 0, "params": {"l1": -1}})
    assert r.status_code == 422
    # 空路径
    r = client.post("/ik/path", json={"targets": []})
    assert r.status_code == 422


def test_invalid_limit_range_returns_422():
    r = client.post(
        "/ik",
        json={
            "target": {"x": 1.0, "y": 1.0},
            "params": {"theta1_min": 2.0, "theta1_max": 1.0},
        },
    )
    assert r.status_code == 422


def test_acceptance_listing():
    r = client.get("/synthetic/acceptance")
    data = r.json()
    names = [c["name"] for c in data["cases"]]
    assert "elbow_flip" in names
    assert "full_extension" in names
    assert "unreachable" in names
    assert "unreachable_gap" in names
