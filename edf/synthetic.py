"""Synthetic trajectory and sensor data generation.

No hardware, no ROS: a synthetic 2D world (border walls plus random
axis-aligned boxes), a synthetic robot trajectory, and a simple synthetic
lidar (fixed-step ray casting on the occupancy grid).  The obstacle cells
accumulated along the trajectory are exactly the kind of input the distance
field CLI consumes.

Run as a script to emit a ready-to-use CLI request on stdout:

    python -m edf.synthetic --seed 7 > request.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from typing import Dict, List, Sequence, Tuple

Pose = Tuple[float, float, float]  # x [m], y [m], heading [rad]
Point = Tuple[float, float]  # x [m], y [m]


def synthetic_world(
    seed: int, width: int, height: int, n_boxes: int = 4
) -> List[List[bool]]:
    """Border walls plus ``n_boxes`` random solid boxes; row-major [y][x]."""
    rng = random.Random(seed)
    world = [[False] * width for _ in range(height)]
    for x in range(width):
        world[0][x] = world[height - 1][x] = True
    for y in range(height):
        world[y][0] = world[y][width - 1] = True
    for _ in range(n_boxes):
        bw = rng.randint(1, max(1, width // 6))
        bh = rng.randint(1, max(1, height // 6))
        x0 = rng.randint(1, max(1, width - bw - 1))
        y0 = rng.randint(1, max(1, height - bh - 1))
        for y in range(y0, min(height - 1, y0 + bh)):
            for x in range(x0, min(width - 1, x0 + bw)):
                world[y][x] = True
    return world


def synthetic_trajectory(
    seed: int, cell_size: Tuple[float, float], width: int, height: int,
    n_poses: int = 12,
) -> List[Pose]:
    """A smooth lawn-mower-ish path kept inside the map, in metres."""
    rng = random.Random(seed + 1)
    sx, sy = cell_size
    poses: List[Pose] = []
    for i in range(n_poses):
        t = i / max(1, n_poses - 1)
        x = (0.2 + 0.6 * t) * width * sx
        y = (0.5 + 0.3 * math.sin(2.0 * math.pi * t)) * height * sy
        heading = rng.uniform(-math.pi, math.pi) if i == 0 else poses[-1][2] + rng.uniform(-0.4, 0.4)
        poses.append((x, y, heading))
    return poses


def cast_ray(
    world: Sequence[Sequence[bool]],
    cell_size: Tuple[float, float],
    origin: Point,
    angle: float,
    max_range: float,
) -> Point | None:
    """Fixed-step ray march; returns the first occupied hit point or None."""
    sx, sy = cell_size
    height = len(world)
    width = len(world[0]) if height else 0
    step = 0.25 * min(sx, sy)
    dx, dy = math.cos(angle), math.sin(angle)
    dist = step
    while dist <= max_range:
        px = origin[0] + dist * dx
        py = origin[1] + dist * dy
        cx = int(px / sx)
        cy = int(py / sy)
        if 0 <= cx < width and 0 <= cy < height:
            if world[cy][cx]:
                return (px, py)
        else:
            return None  # left the map without a hit
        dist += step
    return None


def simulate_mission(
    seed: int = 7,
    width: int = 24,
    height: int = 18,
    cell_size: Tuple[float, float] = (0.5, 0.25),
    n_boxes: int = 4,
    n_poses: int = 12,
    n_beams: int = 36,
    max_range: float = 6.0,
) -> Dict:
    """Build a world, drive the trajectory, scan, and rasterise hits.

    Returns a dict with the trajectory, per-pose hit points (metres), and a
    ``request`` sub-dict that the CLI accepts directly (grid + obstacles).
    """
    world = synthetic_world(seed, width, height, n_boxes)
    trajectory = synthetic_trajectory(seed, cell_size, width, height, n_poses)
    sx, sy = cell_size

    scans: List[List[Point]] = []
    obstacle_cells = set()
    for pose in trajectory:
        hits: List[Point] = []
        for b in range(n_beams):
            angle = pose[2] + 2.0 * math.pi * b / n_beams
            hit = cast_ray(world, cell_size, (pose[0], pose[1]), angle, max_range)
            if hit is not None:
                hits.append(hit)
                cx = min(width - 1, max(0, int(hit[0] / sx)))
                cy = min(height - 1, max(0, int(hit[1] / sy)))
                obstacle_cells.add((cx, cy))
        scans.append(hits)

    obstacles = sorted(obstacle_cells)  # deterministic: sorted [x, y] pairs
    return {
        "seed": seed,
        "trajectory": [[round(p[0], 6), round(p[1], 6), round(p[2], 6)] for p in trajectory],
        "scans": [
            [[round(hx, 6), round(hy, 6)] for hx, hy in hits] for hits in scans
        ],
        "request": {
            "width": width,
            "height": height,
            "cell_size": [sx, sy],
            "obstacles": [[x, y] for x, y in obstacles],
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a synthetic lidar mission and print a distance-field request."
    )
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--width", type=int, default=24)
    parser.add_argument("--height", type=int, default=18)
    parser.add_argument("--cell-size", type=float, nargs=2, default=[0.5, 0.25],
                        metavar=("SX", "SY"))
    parser.add_argument("--full", action="store_true",
                        help="emit the whole mission (trajectory + scans), not just the request")
    args = parser.parse_args(argv)

    mission = simulate_mission(
        seed=args.seed, width=args.width, height=args.height,
        cell_size=(args.cell_size[0], args.cell_size[1]),
    )
    payload = mission if args.full else mission["request"]
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
