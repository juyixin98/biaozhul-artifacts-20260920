"""API-level tests via FastAPI TestClient."""

import numpy as np
from fastapi.testclient import TestClient

from app.main import app
from app.synthetic import simulate_wall_scan
from app.transforms import apply, se2
from app.interpolation import interpolate_pose

client = TestClient(app)


def _scan_payload(scan, pose_times=None, pose_xytheta=None, reference_time=None):
    pose_times = scan.pose_times if pose_times is None else pose_times
    pose_xytheta = scan.pose_xytheta if pose_xytheta is None else pose_xytheta
    return {
        "reference_time": (
            scan.reference_time if reference_time is None else reference_time
        ),
        "extrinsic": {
            "x": scan.extrinsic[0],
            "y": scan.extrinsic[1],
            "theta": scan.extrinsic[2],
        },
        "poses": [
            {"time": float(t), "x": float(x), "y": float(y), "theta": float(th)}
            for t, (x, y, th) in zip(pose_times, pose_xytheta)
        ],
        "points": [
            {"time": float(t), "angle": float(a), "range": float(r)}
            for t, a, r in zip(scan.point_times, scan.angles, scan.ranges)
        ],
    }


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_deskew_endpoint_reduces_wall_residual():
    scan = simulate_wall_scan()
    resp = client.post("/deskew", json=_scan_payload(scan))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["frame"] == "laser@reference_time"

    corrected = np.array([[p["x"], p["y"]] for p in body["points"]])
    ref_pose = interpolate_pose(
        scan.pose_times, scan.pose_xytheta, np.array([scan.reference_time])
    )[0]
    t_world_laser_ref = se2(*ref_pose) @ se2(*scan.extrinsic)

    fixed_world = apply(t_world_laser_ref, corrected)
    fixed_res = np.abs(fixed_world[:, 0] - scan.wall_x)

    p_laser = np.column_stack(
        [scan.ranges * np.cos(scan.angles), scan.ranges * np.sin(scan.angles)]
    )
    naive_world = apply(t_world_laser_ref, p_laser)
    naive_res = np.abs(naive_world[:, 0] - scan.wall_x)

    assert fixed_res.max() < 1e-8
    assert fixed_res.mean() < naive_res.mean() * 1e-3


def test_deskew_endpoint_rejects_missing_coverage():
    scan = simulate_wall_scan()
    half = len(scan.pose_times) // 2
    payload = _scan_payload(
        scan,
        pose_times=scan.pose_times[:half],
        pose_xytheta=scan.pose_xytheta[:half],
    )
    resp = client.post("/deskew", json=payload)
    assert resp.status_code == 422
    assert "outside pose coverage" in resp.json()["detail"]


def test_deskew_endpoint_rejects_uncovered_reference_time():
    scan = simulate_wall_scan()
    payload = _scan_payload(scan, reference_time=scan.pose_times[-1] + 1.0)
    resp = client.post("/deskew", json=payload)
    assert resp.status_code == 422
