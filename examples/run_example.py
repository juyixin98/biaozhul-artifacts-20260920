"""Offline replay example: synthetic straight-wall scan, deskewed via the
service API (in-process TestClient, no real hardware, no network needed).

Run from the repo root:

    python examples/run_example.py
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.deskew import deskew_scan
from app.main import app
from app.synthetic import naive_scan_points, simulate_wall_scan, wall_rmse_in_world


def main() -> None:
    scan = simulate_wall_scan(
        wall_x=5.0,
        n_beams=361,
        velocity=(1.0, 0.3),   # m/s, uniform translation
        omega=0.5,             # rad/s, uniform rotation
        extrinsic=(0.10, 0.02, 0.0),
        reference_time=0.0,
    )

    # 1) Direct library call.
    corrected = deskew_scan(
        ranges=scan.ranges,
        angles=scan.angles,
        point_times=scan.point_times,
        pose_times=scan.pose_times,
        poses=scan.poses,
        reference_time=scan.reference_time,
        extrinsic=scan.extrinsic,
    )
    rmse_naive = wall_rmse_in_world(naive_scan_points(scan), scan)
    rmse_corrected = wall_rmse_in_world(corrected, scan)
    print("=== synthetic wall scan (uniform translation + rotation) ===")
    print(f"wall at x = {scan.wall_x} m, {len(scan.ranges)} beams, "
          f"scan duration {scan.point_times[-1] - scan.point_times[0]:.3f} s")
    print(f"RMSE to wall, raw distorted cloud : {rmse_naive:.6f} m")
    print(f"RMSE to wall, deskewed cloud      : {rmse_corrected:.3e} m")

    # 2) Same request through the HTTP API (in-process).
    client = TestClient(app)
    payload = {
        "ranges": scan.ranges.tolist(),
        "angles": scan.angles.tolist(),
        "point_times": scan.point_times.tolist(),
        "poses": [
            {"t": float(t), "x": float(x), "y": float(y), "theta": float(th)}
            for t, (x, y, th) in zip(scan.pose_times, scan.poses)
        ],
        "reference_time": float(scan.reference_time),
        "extrinsic": {"x": 0.10, "y": 0.02, "theta": 0.0},
    }
    resp = client.post("/deskew", json=payload)
    resp.raise_for_status()
    api_points = np.asarray(resp.json()["points"])
    print(f"API /deskew RMSE to wall          : "
          f"{wall_rmse_in_world(api_points, scan):.3e} m")

    # 3) Insufficient pose coverage must be rejected, not extrapolated.
    keep = scan.pose_times <= scan.point_times.max() - 0.02
    payload["poses"] = [p for p, k in zip(payload["poses"], keep) if k]
    resp = client.post("/deskew", json=payload)
    print(f"\ncoverage-gapped poses -> HTTP {resp.status_code}: "
          f"{resp.json()['detail']}")


if __name__ == "__main__":
    main()
