"""FastAPI 接口测试（TestClient 离线运行）。"""

from fastapi.testclient import TestClient

from mrcbs.main import app

client = TestClient(app)

INTERSECTION_BODY = {
    "grid": ["...", "...", "..."],
    "agents": [
        {"start": [1, 0], "goal": [1, 2]},
        {"start": [0, 1], "goal": [2, 1]},
    ],
}


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200 and resp.json()["status"] == "ok"


def test_solve_intersection():
    resp = client.post("/solve", json={**INTERSECTION_BODY, "check_brute_force": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "optimal"
    assert data["cost"] == 5
    assert data["brute_force_cost"] == 5
    assert data["optimal_verified"] is True
    # 时刻表可重放：等长、逐时刻格子、终点即目标
    tt = data["timetable"]
    assert len(tt) == 2
    assert len(tt[0]) == len(tt[1]) == data["makespan"] + 1
    assert tuple(tt[0][-1]) == (1, 2) and tuple(tt[1][-1]) == (2, 1)
    assert data["stats"]["nodes_expanded"] >= 1


def test_solve_unsolvable():
    body = {
        "grid": ["..."],
        "agents": [
            {"start": [0, 0], "goal": [0, 2]},
            {"start": [0, 2], "goal": [0, 0]},
        ],
    }
    resp = client.post("/solve", json=body)
    assert resp.status_code == 200
    assert resp.json()["status"] == "unsolvable"


def test_solve_invalid_grid():
    body = {**INTERSECTION_BODY, "grid": ["..", "..."]}
    resp = client.post("/solve", json=body)
    assert resp.status_code == 422


def test_validate_endpoint():
    resp = client.post("/solve", json=INTERSECTION_BODY)
    tt = resp.json()["timetable"]
    resp = client.post("/validate", json={**INTERSECTION_BODY, "timetable": tt})
    assert resp.status_code == 200
    assert resp.json()["valid"] is True

    # 人为制造冲突：双方同时进中心格
    bad = {
        **INTERSECTION_BODY,
        "timetable": [
            [[1, 0], [1, 1], [1, 2]],
            [[0, 1], [1, 1], [2, 1]],
        ],
    }
    resp = client.post("/validate", json=bad)
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False and data["violations"]
