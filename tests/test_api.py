"""FastAPI 协议测试。"""

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
EX = Path(__file__).resolve().parent.parent / "examples"


def test_health_and_limits():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    r = client.get("/limits")
    body = r.json()
    assert body["max_points"] == 100
    assert body["max_iter_hard_limit"] == 500


@pytest.mark.parametrize("name", [
    "narrow_corridor.json",
    "angle_cutting.json",
    "duplicate_points.json",
    "scale_micro.json",
    "scale_macro.json",
])
def test_smooth_endpoint_fixtures(name):
    req = json.loads((EX / name).read_text(encoding="utf-8"))
    req["save_run"] = False
    r = client.post("/smooth", json=req)
    assert r.status_code == 200, (name, r.text)
    body = r.json()
    assert body["success"] is True
    assert body["result"] == "smoothed"
    assert body["endpoints_fixed"] is True
    assert len(body["points"]) == len(req["points"])
    assert body["integrity_sha256"]


def test_integrity_hash_is_real_sha256():
    req = json.loads((EX / "duplicate_points.json").read_text(encoding="utf-8"))
    req["save_run"] = False
    body = client.post("/smooth", json=req).json()
    claimed = body.pop("integrity_sha256")
    expected = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert claimed == expected


def test_infeasible_returns_200_with_original():
    """不可行是业务结果而非 HTTP 错误：200 + success=false + 原路径。"""
    req = json.loads((EX / "narrow_corridor.json").read_text(encoding="utf-8"))
    req.update({"max_curvature": 1e-9, "deviation_bound": 0.01, "save_run": False})
    r = client.post("/smooth", json=req)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert body["result"] == "original"
    assert body["points"] == req["points"]


def test_validation_errors_422():
    # 点数超限。
    r = client.post("/smooth", json={
        "points": [[i, 0] for i in range(101)],
        "deviation_bound": 1.0, "max_curvature": 1.0,
    })
    assert r.status_code == 422

    # 非法矩形。
    r = client.post("/smooth", json={
        "points": [[0, 0], [1, 1]],
        "obstacles": [[2, 2, 1, 1]],
        "deviation_bound": 1.0, "max_curvature": 1.0,
    })
    assert r.status_code == 422

    # 缺必填字段。
    r = client.post("/smooth", json={"points": [[0, 0], [1, 1]]})
    assert r.status_code == 422

    # NaN 坐标（NaN 不是合法 JSON，用原始内容投递并期望被拒绝）。
    r = client.post(
        "/smooth",
        content='{"points": [[0, 0], [NaN, 1]], "deviation_bound": 1.0,'
                ' "max_curvature": 1.0}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422
