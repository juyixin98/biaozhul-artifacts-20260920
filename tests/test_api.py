"""HTTP-level tests with FastAPI's in-process TestClient (real uvicorn app)."""

import hashlib
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402
from app.segmentation.synth import build_scene  # noqa: E402

client = TestClient(app)


def _payload(scene_name, **opts):
    scene = build_scene(scene_name)
    body = {
        "points": [
            {"id": i, "x": float(p[0]), "y": float(p[1]), "z": float(p[2])}
            for i, p in enumerate(scene.points)
        ]
    }
    body.update(opts)
    return scene, body


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_segment_mixed_scene_shape_and_ids():
    scene, body = _payload("mixed_world")
    r = client.post("/segment", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["point_count"] == len(scene.points)
    ids = [item["id"] for item in data["results"]]
    assert ids == list(range(len(scene.points)))
    for item in data["results"]:
        assert item["label"] in ("ground", "non_ground", "unknown")
        assert item["confidence"] in ("strong", "weak")
        assert item["vote_count"] >= 1
    # At least one chunk overlapped (vote_count>1 occurs somewhere).
    assert any(item["vote_count"] > 1 for item in data["results"])
    # Checksum is a real sha256 of the canonical point list.
    canonical = json.dumps(
        [[p["id"], p["x"], p["y"], p["z"]] for p in body["points"]],
        separators=(",", ":"), sort_keys=True,
    ).encode()
    assert data["request_sha256"] == hashlib.sha256(canonical).hexdigest()


def test_segment_labels_match_truth_for_example_file():
    # Exercises the same JSON shape shipped in examples/.
    ex_path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "examples", "flat_with_wall.json")
    truth_path = ex_path.replace(".json", ".truth.json")
    with open(ex_path) as fh:
        body = json.load(fh)
    with open(truth_path) as fh:
        truth = json.load(fh)["labels"]
    r = client.post("/segment", json=body)
    assert r.status_code == 200
    pred = [item["label"] for item in r.json()["results"]]
    tp = sum(p == t == "ground" for p, t in zip(pred, truth))
    fn = sum(p != "ground" and t == "ground" for p, t in zip(pred, truth))
    fp = sum(p == "ground" and t == "non_ground" for p, t in zip(pred, truth))
    precision = tp / (tp + fp)
    recall = tp / (tp + fn)
    assert precision >= 0.95
    assert recall >= 0.95


def test_sparse_scene_returns_unknown_via_api():
    _, body = _payload("sparse")
    r = client.post("/segment", json=body)
    assert r.status_code == 200
    assert all(item["label"] == "unknown" for item in r.json()["results"])


def test_steep_slope_refused_via_api():
    _, body = _payload("steep_slope")
    r = client.post("/segment", json=body)
    data = r.json()
    assert all(item["label"] == "unknown" for item in data["results"])
    reasons = {c["reason"] for c in data["chunk_reports"]}
    assert "tilt_exceeded" in reasons


def test_rejects_non_finite_coordinates():
    # Raw body: Python's json module emits the non-standard NaN token, which
    # stdlib parsing accepts; Pydantic must reject it at validation.
    body = (
        '{"points": ['
        '{"id": 0, "x": NaN, "y": 0, "z": 0},'
        '{"id": 1, "x": 0, "y": 0, "z": 0},'
        '{"id": 2, "x": 0, "y": 0, "z": 0}'
        ']}'
    )
    r = client.post("/segment", content=body,
                    headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert "finite" in r.text


def test_rejects_duplicate_ids():
    body = {"points": [{"id": 0, "x": 0, "y": 0, "z": 0},
                       {"id": 0, "x": 1, "y": 0, "z": 0},
                       {"id": 2, "x": 0, "y": 1, "z": 0}]}
    r = client.post("/segment", json=body)
    assert r.status_code == 422


def test_rejects_empty_and_bad_params():
    assert client.post("/segment", json={"points": []}).status_code == 422
    _, body = _payload("sparse")
    body["ransac"] = {"max_tilt_deg": 95.0}
    assert client.post("/segment", json=body).status_code == 422
    _, body2 = _payload("sparse")
    body2["chunk"] = {"overlap": 1.5}
    assert client.post("/segment", json=body2).status_code == 422


def test_custom_parameters_change_gate_behaviour():
    # A 32-degree plane with the default 20-degree gate is refused;
    # raising the gate to 40 degrees makes it decidable.
    _, body = _payload("steep_slope")
    body["ransac"] = {"max_tilt_deg": 40.0}
    r = client.post("/segment", json=body)
    data = r.json()
    assert any(c["status"] == "reliable" for c in data["chunk_reports"])
    assert any(item["label"] == "ground" for item in data["results"])


def test_openapi_available():
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert "/segment" in r.json()["paths"]
