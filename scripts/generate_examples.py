"""Generate the example request payloads under examples/.

Run:  .venv/bin/python scripts/generate_examples.py

Scenarios (estimate relative to ground truth):
  rigid_shift.json        SE(3) shifted/rotated copy -> near-zero ATE
  scale_drift.json        1.2x scale, rigid vs similarity differ
  missing_frames.json     30% of estimate frames dropped -> partial coverage
  rotation_branch_cut.json yaw jumping the +/-pi boundary
  error_duplicate_ts.json duplicate timestamp -> 400 zero_matches-free error
  error_zero_matches.json time ranges disjoint -> 400 zero_matches
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rotation import matrix_to_quat, quat_to_matrix

OUT = Path(__file__).resolve().parents[1] / "examples"


def qz(angle: float) -> list[float]:
    return [float(np.cos(angle / 2)), 0.0, 0.0, float(np.sin(angle / 2))]


def ground_truth(n: int = 30, dt: float = 0.5) -> list[dict]:
    poses = []
    for i in range(n):
        a = i * 0.35
        poses.append({
            "timestamp": round(i * dt, 6),
            "position": [
                round(2.0 * np.cos(a), 6),
                round(2.0 * np.sin(a), 6),
                round(0.05 * i, 6),
            ],
            "orientation": [round(v, 9) for v in qz(a + np.pi / 2)],
        })
    return poses


def transform(poses, R=None, t=None, s=1.0):
    R = np.eye(3) if R is None else np.asarray(R)
    t = np.zeros(3) if t is None else np.asarray(t)
    out = []
    for p in poses:
        pos = s * R @ np.array(p["position"]) + t
        q = matrix_to_quat(R @ quat_to_matrix(np.array(p["orientation"])))
        out.append({
            "timestamp": p["timestamp"],
            "position": [round(float(v), 6) for v in pos],
            "orientation": [round(float(v), 9) for v in q],
        })
    return out


def zrot(a):
    c, s = np.cos(a), np.sin(a)
    return [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]


def wrap(estimated, gt, mode="rigid", rpe=None, gate=0.02):
    return {
        "estimated": estimated,
        "ground_truth": gt,
        "max_time_diff": gate,
        "align_mode": mode,
        "rpe": rpe or [
            {"delta": 2.0, "tolerance": 0.05},
            {"delta": 5.0, "tolerance": 0.1},
        ],
    }


def main() -> None:
    OUT.mkdir(exist_ok=True)
    gt = ground_truth()

    # 1. rigid shift: rotated + translated, exact otherwise
    rigid_est = transform(gt, R=zrot(0.25), t=np.array([1.0, -0.8, 0.3]))
    (OUT / "rigid_shift.json").write_text(
        json.dumps(wrap(rigid_est, gt), indent=2))

    # 2. scale drift: 1.2x
    scale_est = transform(gt, R=zrot(-0.1), t=np.array([0.2, 0.1, 0.0]),
                          s=1.2)
    (OUT / "scale_drift.json").write_text(
        json.dumps(wrap(scale_est, gt, mode="similarity"), indent=2))

    # 3. missing frames: keep ~70% of estimate samples
    kept = [p for i, p in enumerate(rigid_est) if i % 10 not in (2, 5, 8)]
    (OUT / "missing_frames.json").write_text(
        json.dumps(wrap(kept, gt), indent=2))

    # 4. rotation across the +/-pi cut
    cut_gt, cut_est = [], []
    for i in range(12):
        base = {"timestamp": round(i * 0.5, 6),
                "position": [round(float(i), 6),
                             round(0.4 * np.sin(i), 6), 0.0]}
        cut_gt.append({**base, "orientation": qz(np.pi - 0.08)})
        cut_est.append({**base, "orientation": qz(-np.pi + 0.05)})
    (OUT / "rotation_branch_cut.json").write_text(
        json.dumps(wrap(cut_est, cut_gt, rpe=[]), indent=2))

    # 5. error case: duplicate timestamp inside the estimate stream
    dup = [dict(p) for p in rigid_est[:8]]
    dup[5] = dict(dup[4])  # repeat timestamp
    (OUT / "error_duplicate_ts.json").write_text(
        json.dumps(wrap(dup, gt[:8]), indent=2))

    # 6. error case: disjoint time ranges
    far = [{**p, "timestamp": p["timestamp"] + 1000.0} for p in rigid_est[:6]]
    (OUT / "error_zero_matches.json").write_text(
        json.dumps(wrap(far, gt[:6], gate=0.1), indent=2))

    print(f"wrote {len(list(OUT.glob('*.json')))} examples to {OUT}")


if __name__ == "__main__":
    main()
