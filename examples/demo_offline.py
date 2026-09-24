"""Offline synthetic-data demo (no hardware, no visualisation).

Runs a scripted sequence of rays against a small grid, prints the state before
and after, exercises no-return / out-of-map / negative-coordinate cases, and
demonstrates export -> reload. Run with:

    python examples/demo_offline.py
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from occupancy_grid import CellState, OccupancyGrid, RayObservation
from occupancy_grid.grid import GridConfig

GLYPHS = {
    CellState.UNKNOWN.value: "?",
    CellState.FREE.value: ".",
    CellState.OCCUPIED.value: "#",
}


def render(grid: OccupancyGrid) -> str:
    states = grid.state_map()
    lines = []
    for iy in range(grid.config.ny - 1, -1, -1):  # y increases upward
        lines.append("  " + " ".join(GLYPHS[s] for s in states[iy]))
    header = "    " + " ".join(str(ix) for ix in range(grid.config.nx))
    return header + "\n" + "\n".join(lines)


def section(title: str) -> None:
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


def main() -> None:
    section("1. Build 6x5 grid, origin (-3,-2), 1 m cells; all cells unknown")
    grid = OccupancyGrid(GridConfig(nx=6, ny=5, origin_x=-3.0, origin_y=-2.0))
    print(render(grid))

    section("2. Echo ray (-2.5,-0.5) -> (1.5,-0.5): endpoint cell (4,1) occupied")
    result = grid.update_ray(RayObservation(ox=-2.5, oy=-0.5, ex=1.5, ey=-0.5))
    print("free cells    :", result["free_cells"])
    print("occupied cell :", result["occupied_cell"])
    print(render(grid))

    section("3. Repeat the same ray 5 times: evidence accumulates, then saturates")
    for k in range(1, 6):
        grid.update_ray(RayObservation(ox=-2.5, oy=-0.5, ex=1.5, ey=-0.5))
        p = grid.probability_at(4, 1)
        print(f"  after {k + 1} observations, P(occ) at (4,1) = {p:.4f}")

    section("4. No-return ray upward from (-2.5,-1.5), range 3: free only")
    result = grid.update_no_return(-2.5, -1.5, angle=math.pi / 2, max_range=3.0)
    print("free cells:", result["free_cells"])
    print(render(grid))

    section("5. Echo far outside the map (x=50): occupied dropped, free kept")
    result = grid.update_ray(RayObservation(ox=-2.5, oy=0.5, ex=50.0, ey=0.5))
    print("endpoint in map:", result["endpoint_in_map"],
          "| free cells:", result["free_cells"])
    print(render(grid))

    section("6. Negative-coordinate echo at (-2.5,-1.5) -> cell (0,0)")
    result = grid.update_ray(RayObservation(ox=-2.5, oy=-1.5, ex=-2.5, ey=-1.5))
    print("occupied cell:", result["occupied_cell"])
    print("note: (0,0) was marked free by the no-return ray in step 4, so one")
    print("      occupied vote just cancels it toward prior (it stays free).")
    print(f"      P(occ) at (0,0) = {grid.probability_at(0, 0):.4f}")
    print(render(grid))

    section("7. Export to JSON, build a new grid from it, states identical")
    data = grid.to_dict()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "grid.json"
        path.write_text(json.dumps(data, indent=2))
        size = path.stat().st_size
        restored = OccupancyGrid.from_dict(json.loads(path.read_text()))
    print(f"exported {size} bytes; reload state maps equal: "
          f"{restored.state_map() == grid.state_map()}")

    section("8. Final probabilities of the first echo line (row iy=1)")
    for ix in range(6):
        print(f"  cell ({ix},1): P(occ) = {grid.probability_at(ix, 1):.4f}"
              f"  state = {grid.state_at(ix, 1).value}")


if __name__ == "__main__":
    main()
