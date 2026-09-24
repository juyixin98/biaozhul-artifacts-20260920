"""FastAPI 接口测试。"""

from fastapi.testclient import TestClient

from main import app
from mapmatch import synthetic

client = TestClient(app)


def _graph_payload(graph):
    return {
        "nodes": [{"id": nid, "x": n.x, "y": n.y} for nid, n in graph.nodes.items()],
        "edges": [
            {"id": e.id, "u": e.u, "v": e.v, "polyline": [list(p) for p in e.points]}
            for e in graph.edges.values()
        ],
    }


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_match_overpass():
    graph = synthetic.make_overpass_graph()
    ts, xs, ys, _ = synthetic.make_overpass_trajectory()
    body = {
        "graph": _graph_payload(graph),
        "trajectory": [{"t": t, "x": x, "y": y} for t, x, y in zip(ts, xs, ys)],
        "params": {"sigma": 10, "beta": 8, "candidate_radius": 50, "max_time_gap": 10},
    }
    r = client.post("/match", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    matched = [p for p in data["points"] if p["matched"]]
    assert len(matched) >= 0.95 * len(ts)
    assert all(p["edge_id"].startswith("HE") for p in matched)
    assert "unmatched_intervals" in data and "time_gaps" in data
    assert all(0.0 <= p["confidence_margin"] <= 1.0 for p in matched)


def test_match_empty_trajectory_400():
    graph = synthetic.make_gap_graph()
    body = {"graph": _graph_payload(graph), "trajectory": []}
    r = client.post("/match", json=body)
    assert r.status_code == 400
