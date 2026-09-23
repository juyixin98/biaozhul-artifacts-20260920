"""Generate deterministic example request bodies under examples/.

Each JSON file is a ready-to-sign request payload. Run:
    .venv/bin/python scripts/gen_examples.py
"""

from __future__ import annotations

import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import robot  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "examples")


def body(R, p, current=None):
    return {
        "position": [float(x) for x in np.asarray(p, dtype=float)],
        "rotation": [[float(x) for x in row]
                     for row in np.asarray(R, dtype=float)],
        "current_joints": None if current is None
        else [float(x) for x in np.asarray(current, dtype=float)],
        "seed": 20260923,
    }


def main() -> None:
    os.makedirs(OUT, exist_ok=True)

    # 1. known forward-kinematics target (expected: ok, multiple branches)
    q = robot.clamp_to_limits(
        np.deg2rad([37.0, 30.0, -2.2, 77.2, -38.2, -98.1]))
    R, p = robot.fk(q)
    _write("01_known_fk_target.json", body(R, p, current=q))
    with open(os.path.join(OUT, "01_known_fk_source_joints.json"), "w") as f:
        json.dump({"joint_angles_rad": [float(x) for x in q],
                   "joint_angles_deg": [float(x) for x in np.rad2deg(q)]},
                  f, indent=2)

    # 2. fully extended straight singular pose (exact target -> ok)
    qs = np.array([0.0, 0.0, -math.pi / 2, 0.0, 0.0, 0.0])
    Rs, ps = robot.fk(qs)
    _write("02_straight_singular_exact.json", body(Rs, ps))

    # 3. same pose pushed 5 mm radially beyond max reach in the singular
    #    direction (expected: singular_no_convergence)
    po = ps.copy()
    f = 1.0 + 0.005 / math.hypot(ps[0], ps[1])
    po[0] *= f
    po[1] *= f
    _write("03_near_singular_no_convergence.json", body(Rs, po))

    # 4. far outside the workspace (expected: unreachable)
    _write("04_out_of_reach.json",
            body(np.eye(3), np.array([3.0, 0.0, 0.2])))

    # 5. base azimuth 181 deg — forces q1 past +/-160 deg on every branch
    #    (expected: joint_limit_conflict)
    a = math.radians(181.0)
    p5 = np.array([0.45 * math.cos(a), 0.45 * math.sin(a), 0.10])
    _write("05_joint_limit_conflict.json", body(np.eye(3), p5))

    print("wrote examples to", OUT)


def _write(name: str, payload: dict) -> None:
    with open(os.path.join(OUT, name), "w") as f:
        json.dump(payload, f, indent=2)
    print(" -", name)


if __name__ == "__main__":
    main()
