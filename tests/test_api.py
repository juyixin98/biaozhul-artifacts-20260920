"""FastAPI 离线服务测试（使用 httpx + ASGI transport，不监听真实端口）。"""

import numpy as np
from fastapi.testclient import TestClient

from speed_profile.api import app
from speed_profile import scenarios

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_list_and_get_scenario():
    r = client.get("/api/scenarios")
    assert r.status_code == 200
    assert "hairpin" in r.json()["scenarios"]

    r = client.get("/api/scenarios/hairpin")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "hairpin"
    assert len(body["input"]["s"]) == len(body["input"]["kappa"])


def test_unknown_scenario_404():
    r = client.get("/api/scenarios/nope")
    assert r.status_code == 404


def test_parameterize_straight_roundtrip():
    sc = scenarios.straight_line(length=20.0, n=41)
    payload = {
        "s": sc["s"].tolist(),
        "kappa": sc["kappa"].tolist(),
        "v_max": sc["v_max"],
        "a_max": sc["a_max"],
        "a_min": sc["a_min"],
        "a_lat_max": None,
        "v_start": 0.0,
        "v_end": 0.0,
    }
    r = client.post("/api/parameterize", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feasible"] is True
    assert body["v"][0] == 0.0 and body["v"][-1] == 0.0
    assert len(body["v"]) == 41
    assert len(body["a_seg"]) == 40
    assert body["total_time"] > 0.0


def test_parameterize_invalid_input_400():
    r = client.post("/api/parameterize", json={
        "s": [0.0, 1.0, 0.5],
        "kappa": [0.0, 0.0, 0.0],
    })
    assert r.status_code == 400
    assert "非递减" in r.json()["detail"]


def test_parameterize_schema_rejects_bad_shape():
    # s 与 kappa 长度不一致 -> 400（pydantic 类型合法，业务校验拒绝）
    r = client.post("/api/parameterize", json={
        "s": [0.0, 1.0, 2.0],
        "kappa": [0.0, 0.0],
    })
    assert r.status_code == 400


def test_parameterize_with_array_vmax():
    s = np.linspace(0, 10, 11).tolist()
    r = client.post("/api/parameterize", json={
        "s": s,
        "kappa": [0.0] * 11,
        "v_max": [5.0] * 11,
        "a_max": 2.0,
        "a_min": -2.0,
        "a_lat_max": None,
    })
    assert r.status_code == 200
    assert r.json()["feasible"] is True


def test_nan_serialized_as_null_for_zero_length_segment():
    r = client.post("/api/parameterize", json={
        "s": [0.0, 5.0, 5.0, 10.0],
        "kappa": [0.0, 0.0, 0.0, 0.0],
        "v_max": 6.0,
        "a_max": 2.0,
        "a_min": -2.0,
        "a_lat_max": None,
        "v_start": 0.0,
        "v_end": 0.0,
    })
    assert r.status_code == 200
    body = r.json()
    # 第 2 段是零长度段：dt=0，a_seg 为 null
    assert body["dt"][1] == 0.0
    assert body["a_seg"][1] is None
