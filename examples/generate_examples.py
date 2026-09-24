"""Generate example request payloads for the trajectory evaluation API.

Run from the repository root:
    python examples/generate_examples.py

Writes several ready-to-use JSON files into examples/:
  * pure_translation.json   - rigid offset, should give ~0 error
  * scale_drift.json        - 1.3x scale drift; compare rigid vs similarity
  * missing_measurements.json - sparse estimates, demonstrates coverage
  * rotation_crossover.json - quaternions crossing the +/- sign seam
  * err_duplicate_timestamp.json / err_no_matches.json /
    err_degenerate_alignment.json - requests the API must reject with 422
"""

from __future__ import annotations

import json
import os

import numpy as np

from app.geometry import quat_to_rot, rot_to_quat
from tests._helpers import q_from_axis_angle

OUT_DIR = os.path.dirname(os.path.abspath(__file__))

# Planar rounded-rectangle ground truth: non-degenerate (rank 2), full 360 yaw.
N = 16
_T = np.round(np.linspace(0.0, 3.0, N), 4)
_ANG = np.linspace(0.0, 2.0 * np.pi, N, endpoint=False)
_P = np.stack([2.0 * np.cos(_ANG), 1.5 * np.sin(_ANG), 0.3 * np.sin(_ANG / 2)], axis=1)
_Q = [q_from_axis_angle(np.array([0.0, 0.0, 1.0]), a) for a in _ANG]


def _pose(t, p, q):
    return {
        "time": round(float(t), 6),
        "position": [round(float(v), 9) for v in p],
        "quaternion_xyzw": [round(float(v), 9) for v in q],
    }


def _ground_truth():
    return [_pose(t, p, q) for t, p, q in zip(_T, _P, _Q, strict=True)]


def _apply(R, t, s, times, flip=False):
    out = []
    for i, (ts, p, q) in enumerate(zip(times, _P, _Q, strict=True)):
        qr = rot_to_quat(R @ quat_to_rot(q))
        pp = s * R @ p + t
        if flip and i % 2 == 1:
            qr = -qr
        out.append(_pose(ts, pp, qr))
    return out


def _wrap(est, gt, mode="rigid", max_dt=0.02, delta=2):
    return {
        "estimated": est,
        "ground_truth": gt,
        "association": {"max_time_diff": max_dt},
        "alignment": {"mode": mode},
        "rpe": {"delta_index": delta, "tolerance_index": 0},
    }


def main() -> None:
    gt = _ground_truth()

    # 1) Pure rigid offset (0.4 rad yaw + translation), tiny time jitter.
    R1 = quat_to_rot(q_from_axis_angle(np.array([0.0, 0.0, 1.0]), 0.4))
    t1 = np.array([1.0, -0.5, 0.25])
    est1 = _apply(R1, t1, 1.0, _T + 0.004)
    _write("pure_translation.json", _wrap(est1, gt, mode="rigid"))

    # 2) Scale drift of 1.3x plus a rotation/translation.
    R2 = quat_to_rot(q_from_axis_angle(np.array([0.0, 1.0, 0.0]), -0.25))
    t2 = np.array([-0.8, 0.3, 0.1])
    est2 = _apply(R2, t2, 1.3, _T + 0.004)
    _write("scale_drift.json", _wrap(est2, gt, mode="rigid"))
    sim_req = _wrap(est2, gt, mode="similarity")
    _write("scale_drift_similarity.json", sim_req)

    # 3) Missing measurements: only 10 of 16 estimates, slightly drifting time.
    keep = np.linspace(0, N - 1, 10).round().astype(int)
    est3 = [_apply(R1, t1, 1.0, _T)[i] for i in keep]
    for k, p in enumerate(est3):
        p["time"] = round(float(_T[keep[k]] + 0.006 + 0.001 * k), 6)
    _write("missing_measurements.json", _wrap(est3, gt, mode="rigid", delta=1))

    # 4) Rotation cross-over near 180 deg with alternating quaternion signs.
    R4 = quat_to_rot(q_from_axis_angle(np.array([0.0, 1.0, 0.0]), np.deg2rad(172.0)))
    t4 = np.array([0.4, 0.4, -0.1])
    est4 = _apply(R4, t4, 1.0, _T + 0.003, flip=True)
    _write("rotation_crossover.json", _wrap(est4, gt, mode="rigid"))

    # 5) Error cases.
    dup = _wrap(_apply(R1, t1, 1.0, _T), gt)
    dup["estimated"][5]["time"] = dup["estimated"][4]["time"]
    _write("err_duplicate_timestamp.json", dup)

    nomatch = _wrap(_apply(R1, t1, 1.0, _T + 100.0), gt)
    _write("err_no_matches.json", nomatch)

    n = 8
    degen_gt = [
        _pose(float(i), np.array([float(i), 0.0, 0.0]), np.array([0, 0, 0, 1.0]))
        for i in range(n)
    ]
    degen_est = [
        _pose(float(i) + 0.001, np.array([float(i), 0.3, 0.0]), np.array([0, 0, 0, 1.0]))
        for i in range(n)
    ]
    _write("err_degenerate_alignment.json", _wrap(degen_est, degen_gt, delta=1))

    print(f"Wrote example files to {OUT_DIR}")


def _write(name: str, payload: dict) -> None:
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"  - {name}")


if __name__ == "__main__":
    main()
