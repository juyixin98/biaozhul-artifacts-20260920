#!/usr/bin/env python3
"""Generate deterministic example inputs for the registration service.

No ground-truth transform is sent to the server: these files only contain the two
point clouds (and, for one example, an optional rough initial pose). The script
prints the ground truth it used so the server's answer can be checked offline.
"""
import json
import math
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "examples")


def rot_axis(axis, angle):
    x, y, z = axis
    n = math.sqrt(x * x + y * y + z * z)
    x, y, z = x / n, y / n, z / n
    c, s, C = math.cos(angle), math.sin(angle), 1 - math.cos(angle)
    return [
        [c + x * x * C, x * y * C - z * s, x * z * C + y * s],
        [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
        [z * x * C - y * s, z * y * C + x * s, c + z * z * C],
    ]


def matvec(M, v):
    return [sum(M[i][j] * v[j] for j in range(3)) for i in range(3)]


def write(name, payload):
    path = os.path.join(OUT, name)
    with open(path, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"wrote {path} ({len(payload.get('source', []))} source pts)")


def main():
    os.makedirs(OUT, exist_ok=True)
    random.seed(20260923)

    # 1) Known transform, cube cloud.
    n = 300
    base = [[random.uniform(-1, 1) for _ in range(3)] for _ in range(n)]
    R = rot_axis((0.3, 0.7, 0.2), 0.35)
    t = [0.15, -0.10, 0.08]
    moved = [[a + b for a, b in zip(matvec(R, p), t)] for p in base]
    # Two corrupted rows that the server must drop.
    moved_with_bad = moved + [[float("nan"), 1.0, 2.0], [1.0, float("inf"), 0.0]]
    write("known_transform.json", {
        "source": moved_with_bad,
        "target": base,
        "max_iterations": 100,
        "max_correspondence_distance": 0.5,
    })
    # Expected recovered pose maps moved -> base: R_out = R^T, t_out = -R^T t.
    print("  ground truth R^T =", json.dumps([list(row) for row in zip(*R)]))
    print("  ground truth -R^T t =", [-sum(R[j][i] * t[j] for j in range(3)) for i in range(3)])

    # 2) No-overlap fixture: clouds 100 units apart.
    a = [[random.uniform(-0.5, 0.5) for _ in range(3)] for _ in range(100)]
    b = [[v + 100.0 for v in p] for p in a]
    write("no_overlap.json", {
        "source": b, "target": a,
        "max_iterations": 30, "max_correspondence_distance": 0.5,
    })
    print("  expected: convergence_reason=no_overlap, high_confidence=false")

    # 3) Collinear fixture: points on the x-axis (rotation about x unobservable).
    line = [[0.02 * i, 0.0, 0.0] for i in range(100)]
    Rline = rot_axis((1, 0, 0), 0.6)
    line_moved = [[a2 + b2 for a2, b2 in zip(matvec(Rline, p), [0.05, 0, 0])] for p in line]
    write("collinear.json", {
        "source": line_moved, "target": line, "max_iterations": 100,
    })
    print("  expected: convergence_reason=degenerate_geometry, high_confidence=false")


if __name__ == "__main__":
    main()
