"""Offline synthetic example: build a small map from replay-style scans.

No hardware, no visualization. A fake robot drives three poses and emits
laser beams; the grid accumulates log-odds. Prints per-cell state, a
probability slice, and verifies a save/load round-trip.
"""

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from occupancy_grid import CellState, OccupancyGrid, load_grid, save_grid

# Tiny 8x8 grid at 1 m/cell, origin at (-4, -4) so coordinates are signed.
grid = OccupancyGrid(
    width=8, height=8, resolution=1.0, origin_x=-4.0, origin_y=-4.0,
    l_min=-4.0, l_max=4.0,
)

# Three synthetic scans: pose (x, y, theta), beam angles, ranges, max_range.
scans = [
    # Facing +x: wall 3 m ahead across the beam fan.
    (0.0, 0.0, 0.0, [-0.4, 0.0, 0.4], [3.0, 3.0, 3.0], 5.0),
    # Move forward 1 m, same view: wall now 2 m ahead; evidence repeats.
    (1.0, 0.0, 0.0, [-0.4, 0.0, 0.4], [2.0, 2.0, 2.0], 5.0),
    # Same pose: one beam misses (range capped at max_range).
    (1.0, 0.0, 0.0, [0.8], [5.0], 5.0),
]

for i, (x, y, theta, angles, ranges, max_range) in enumerate(scans, start=1):
    n = grid.integrate_scan(x, y, theta, angles, ranges, max_range)
    print(f"scan {i}: {n} cell updates")

states = grid.state_grid()
probs = grid.probability_grid()

print("\nstate map (U=unknown, .=free, #=occupied), x right, y up:")
for iy in range(grid.height - 1, -1, -1):
    row = "".join(
        {CellState.UNKNOWN: "U", CellState.FREE: ".", CellState.OCCUPIED: "#"}[
            int(states[iy, ix])
        ]
        for ix in range(grid.width)
    )
    print(f"y={iy:1d} {row}")

wall_cell = grid.world_to_grid(3.0, 0.0)
if wall_cell is not None:
    ix, iy = wall_cell
    print(f"\nwall cell ({ix},{iy}): log-odds={grid.log_odds[iy, ix]:.3f}, "
          f"p_occ={probs[iy, ix]:.3f}")

# Save / load round-trip.
with tempfile.TemporaryDirectory() as tmp:
    path = save_grid(grid, Path(tmp) / "synthetic_map")
    restored = load_grid(path)
    same = np.array_equal(restored.log_odds, grid.log_odds)
    print(f"\nsaved -> {path.name}; reloaded log-odds identical: {same}")
