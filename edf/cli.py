"""JSON command-line entry point for the Euclidean distance field.

Usage:
    python -m edf.cli request.json [-o response.json]
    cat request.json | python -m edf.cli -          # stdin -> stdout

Request schema (object):
    width      int > 0                     grid width in cells
    height     int > 0                     grid height in cells
    cell_size  [sx, sy]  (optional)        cell size in metres, default [1, 1]
    obstacles  [[x, y], ...]               obstacle cells, 0 <= x < width,
                                           0 <= y < height
    -- or, instead of obstacles --
    occupancy  [[row 0], [row 1], ...]     height rows of width truthy values

Response schema (object):
    width, height, cell_size               echoed
    algorithm                              "felzenszwalb-huttenlocher"
    distances  [[...], ...]                distances[y][x] in metres;
                                           null when the map has no obstacle
    sources    [[...], ...]                sources[y][x] = [col, row] of the
                                           nearest obstacle cell, or null

Unknown request fields are ignored, so a full synthetic mission file (which
carries trajectory/scans next to "request") can be trimmed to its "request"
member or a plain request can carry extra annotations.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

from .distance_field import distance_field

ALGORITHM = "felzenszwalb-huttenlocher"


def load_request(req: Dict[str, Any]) -> Tuple[np.ndarray, Tuple[float, float]]:
    """Validate a request dict; return (occupancy bool array, cell_size)."""
    if not isinstance(req, dict):
        raise ValueError("request must be a JSON object")

    width = req.get("width")
    height = req.get("height")
    if not isinstance(width, int) or isinstance(width, bool) or width <= 0:
        raise ValueError("width must be a positive integer")
    if not isinstance(height, int) or isinstance(height, bool) or height <= 0:
        raise ValueError("height must be a positive integer")

    cell = req.get("cell_size", [1.0, 1.0])
    if (
        not isinstance(cell, list)
        or len(cell) != 2
        or not all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in cell)
    ):
        raise ValueError("cell_size must be [sx, sy] of numbers")
    sx, sy = float(cell[0]), float(cell[1])
    if not (math.isfinite(sx) and sx > 0.0 and math.isfinite(sy) and sy > 0.0):
        raise ValueError("cell_size entries must be positive and finite")

    has_obstacles = "obstacles" in req
    has_occupancy = "occupancy" in req
    if has_obstacles == has_occupancy:
        raise ValueError("provide exactly one of 'obstacles' or 'occupancy'")

    occ = np.zeros((height, width), dtype=bool)
    if has_obstacles:
        obstacles = req["obstacles"]
        if not isinstance(obstacles, list):
            raise ValueError("obstacles must be a list of [x, y] pairs")
        for item in obstacles:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not all(isinstance(v, int) and not isinstance(v, bool) for v in item)
            ):
                raise ValueError(f"obstacle must be [x, y] of integers, got {item!r}")
            x, y = item
            if not (0 <= x < width and 0 <= y < height):
                raise ValueError(
                    f"obstacle [{x}, {y}] out of bounds for {width}x{height} grid"
                )
            occ[y, x] = True
    else:
        occupancy = req["occupancy"]
        if not isinstance(occupancy, list) or len(occupancy) != height:
            raise ValueError(f"occupancy must have exactly {height} rows")
        for y, row in enumerate(occupancy):
            if not isinstance(row, list) or len(row) != width:
                raise ValueError(f"occupancy row {y} must have exactly {width} cells")
            for x, value in enumerate(row):
                occ[y, x] = bool(value)
    return occ, (sx, sy)


def build_response(
    occ: np.ndarray, cell_size: Tuple[float, float]
) -> Dict[str, Any]:
    """Compute the field and serialise it (inf/None -> JSON null)."""
    dist, sources = distance_field(occ, cell_size)
    h, w = occ.shape
    distances: List[List[Any]] = []
    for y in range(h):
        row: List[Any] = []
        for x in range(w):
            d = float(dist[y, x])
            row.append(d if math.isfinite(d) else None)
        distances.append(row)
    return {
        "width": w,
        "height": h,
        "cell_size": [cell_size[0], cell_size[1]],
        "algorithm": ALGORITHM,
        "distances": distances,
        "sources": [
            [list(src) if src is not None else None for src in row]
            for row in sources
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="edf.cli",
        description="Exact Euclidean distance field over a 2D occupancy grid.",
    )
    parser.add_argument("input", help="request JSON file, or '-' for stdin")
    parser.add_argument("-o", "--output", help="response file (default: stdout)")
    args = parser.parse_args(argv)

    try:
        if args.input == "-":
            req = json.load(sys.stdin)
        else:
            with open(args.input, "r", encoding="utf-8") as fh:
                req = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"cannot read request: {exc}"}), file=sys.stderr)
        return 2

    try:
        occ, cell_size = load_request(req)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2

    response = build_response(occ, cell_size)
    text = json.dumps(response, indent=2) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
