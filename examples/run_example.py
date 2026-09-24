"""Offline example: deskew a synthetic scan through the HTTP API.

Uses FastAPI's TestClient, so no server needs to be started — this is a
pure offline replay of synthetic data.

Run:  python examples/run_example.py
"""

import sys
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app
from app.interpolation import interpolate_pose
from app.synthetic import simulate_wall_scan
from app.transforms import apply, se2


def main() -> None:
    scan = simulate_wall_scan(
        vx=0.5, vy=0.1, omega=0.8, n_points=360, range_noise_std=0.005, seed=7
    )
    payload = {
        "reference_time": scan.reference_time,
        "extrinsic": {"x": scan.extrinsic[0], "y": scan.extrinsic[1], "theta": scan.extrinsic[2]},
        "poses": [
            {"time": float(t), "x": float(x), "y": float(y), "theta": float(th)}
            for t, (x, y, th) in zip(scan.pose_times, scan.pose_xytheta)
        ],
        "points": [
            {"time": float(t), "angle": float(a), "range": float(r)}
            for t, a, r in zip(scan.point_times, scan.angles, scan.ranges)
        ],
    }

    client = TestClient(app)
    resp = client.post("/deskew", json=payload)
    resp.raise_for_status()
    corrected = np.array([[p["x"], p["y"]] for p in resp.json()["points"]])

    ref_pose = interpolate_pose(
        scan.pose_times, scan.pose_xytheta, np.array([scan.reference_time])
    )[0]
    t_world_laser_ref = se2(*ref_pose) @ se2(*scan.extrinsic)

    naive = np.column_stack(
        [scan.ranges * np.cos(scan.angles), scan.ranges * np.sin(scan.angles)]
    )
    naive_res = np.abs(apply(t_world_laser_ref, naive)[:, 0] - scan.wall_x)
    fixed_res = np.abs(apply(t_world_laser_ref, corrected)[:, 0] - scan.wall_x)

    print(f"points: {len(scan.ranges)}  (wall at x = {scan.wall_x} m)")
    print(f"before deskew: mean |residual| = {naive_res.mean():.6f} m, "
          f"max = {naive_res.max():.6f} m")
    print(f"after  deskew: mean |residual| = {fixed_res.mean():.6f} m, "
          f"max = {fixed_res.max():.6f} m")

    # Missing-coverage behaviour: drop the second half of the trajectory.
    half = len(scan.pose_times) // 2
    payload["poses"] = payload["poses"][:half]
    resp = client.post("/deskew", json=payload)
    print(f"missing pose coverage -> HTTP {resp.status_code}: {resp.json()['detail']}")


if __name__ == "__main__":
    main()
