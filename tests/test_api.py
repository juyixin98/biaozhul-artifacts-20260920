"""API tests using FastAPI's TestClient (httpx)."""

from fastapi.testclient import TestClient

from app.main import app
from examples.instances import CROSSROAD, CORRIDOR_UNSOLVABLE

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_solve_crossroad():
    r = client.post("/solve", json=CROSSROAD)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "solved"
    assert body["cost"] == 9
    assert body["errors"] == []
    # replayable timetable: every entry is [x, y, t] with t increasing by 1
    for agent_id, rows in body["timetable"].items():
        assert all(len(row) == 3 for row in rows)
        assert [row[2] for row in rows] == list(range(len(rows)))
    assert body["stats"]["high_level_nodes"] >= 1


def test_solve_unsolvable():
    r = client.post("/solve", json=CORRIDOR_UNSOLVABLE)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unsolvable"
    assert body["timetable"] == {}


def test_invalid_geometry_reported():
    bad = {
        "grid": {"width": 3, "height": 3, "obstacles": [[1, 1]]},
        "agents": [
            {"id": "a", "start": [1, 1], "goal": [0, 0]},   # start on obstacle
            {"id": "b", "start": [0, 0], "goal": [9, 9]},   # goal out of bounds
        ],
    }
    r = client.post("/solve", json=bad)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unsolvable"
    assert len(body["errors"]) == 2


def test_schema_validation_error():
    r = client.post("/solve", json={"grid": {"width": 0, "height": 3}, "agents": []})
    assert r.status_code == 422
