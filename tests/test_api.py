"""API tests for the deskew service (in-process via TestClient)."""

import numpy as np
from fastapi.testclient import TestClient

from app.main import app
from app.synthetic import simulate_wall_scan

client = TestClient(app)


def _payload(scan):
    return {
        "ranges": scan.ranges.tolist(),
        "angles": scan.angles.tolist(),
        "point_times": scan.point_times.tolist(),
        "poses": [
            {"t": float(t), "x": float(x), "y": float(y), "theta": float(th)}
            for (t, (x, y, th)) in zip(scan.pose_times, scan.poses)
        ],
        "reference_time": float(scan.reference_time),
        "extrinsic": {
            "x": scan.extrinsic[0],
            "y": scan.extrinsic[1],
            "theta": scan.extrinsic[2],
        },
    }


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_deskew_endpoint_ok():
    scan = simulate_wall_scan()
    resp = client.post("/deskew", json=_payload(scan))
    assert resp.status_code == 200
    body = resp.json()
    assert body["reference_time"] == scan.reference_time
    assert body["frame"] == "laser@reference_time"
    points = np.asarray(body["points"])
    assert points.shape == (len(scan.ranges), 2)
    assert np.all(np.isfinite(points))


def test_deskew_endpoint_rejects_insufficient_coverage():
    scan = simulate_wall_scan()
    keep = scan.pose_times <= scan.point_times.max() - 0.02
    payload = _payload(scan)
    payload["poses"] = [
        p for p, k in zip(payload["poses"], keep) if k
    ]
    resp = client.post("/deskew", json=payload)
    assert resp.status_code == 400
    assert "refusing to extrapolate" in resp.json()["detail"]


def test_deskew_endpoint_rejects_length_mismatch():
    scan = simulate_wall_scan()
    payload = _payload(scan)
    payload["ranges"] = payload["ranges"][:-1]
    resp = client.post("/deskew", json=payload)
    assert resp.status_code == 422
