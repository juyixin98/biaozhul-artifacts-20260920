"""API tests using FastAPI's TestClient (offline, in-process)."""

import math

from fastapi.testclient import TestClient

from hybrid_astar.app import app
from hybrid_astar.scenarios import narrow_corridor, unsolvable

client = TestClient(app)


def _payload(scenario, **planner_overrides):
    return {
        "grid": scenario.grid,
        "resolution": scenario.resolution,
        "start": {"x": scenario.start[0], "y": scenario.start[1],
                  "theta": scenario.start[2]},
        "goal": {"x": scenario.goal[0], "y": scenario.goal[1],
                 "theta": scenario.goal[2]},
        "planner": planner_overrides,
    }


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_plan_narrow_corridor_via_api():
    sc = narrow_corridor()
    r = client.post("/plan", json=_payload(sc))
    assert r.status_code == 200
    body = r.json()
    assert body["success"], body["message"]
    assert body["cost"] > 0
    assert len(body["path"]) > 2
    last = body["path"][-1]
    assert math.hypot(last["x"] - sc.goal[0], last["y"] - sc.goal[1]) <= 0.75
    assert all(p["gear"] in (1, -1) for p in body["path"])


def test_plan_unsolvable_via_api():
    sc = unsolvable()
    r = client.post("/plan", json=_payload(sc))
    assert r.status_code == 200
    body = r.json()
    assert not body["success"]
    assert body["path"] == []


def test_plan_zero_heuristic_via_api():
    sc = narrow_corridor()
    r = client.post("/plan", json=_payload(sc, use_heuristic=False))
    assert r.status_code == 200
    assert r.json()["success"]
