"""Synthetic benchmark scenarios (offline, no hardware).

Shared by the automated tests and the example runner. Each builder
returns the raw occupancy grid plus start/goal poses.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass
class Scenario:
    name: str
    grid: list[list[int]]
    resolution: float
    start: tuple[float, float, float]
    goal: tuple[float, float, float]
    description: str


def _blank(rows: int, cols: int) -> np.ndarray:
    return np.zeros((rows, cols), dtype=np.uint8)


def _add_boundary(g: np.ndarray) -> None:
    g[0, :] = 1
    g[-1, :] = 1
    g[:, 0] = 1
    g[:, -1] = 1


def narrow_corridor() -> Scenario:
    """A wall with a 2 m gap; the vehicle must thread the gap."""
    res = 0.5
    g = _blank(40, 40)  # 20 m x 20 m
    _add_boundary(g)
    g[20, :] = 1          # horizontal wall at y = 10 m
    g[20, 8:12] = 0       # 2 m wide gap at x in [4, 6]
    return Scenario(
        name="narrow_corridor",
        grid=g.tolist(),
        resolution=res,
        start=(5.0, 4.0, math.pi / 2),
        goal=(8.0, 16.0, math.pi / 2),
        description="wall with a 2 m gap; vehicle (1 m wide + margin) must pass",
    )


def reverse_uturn() -> Scenario:
    """Dead-end alley: starts facing the closed end, must reverse out."""
    res = 0.5
    g = _blank(20, 40)  # 20 m wide (x), 10 m tall (y)
    _add_boundary(g)
    # Close everything right of x=10 except a 2 m tall alley at y in [4.5, 6.5].
    g[1:9, 20:39] = 1     # wall below the alley
    g[13:19, 20:39] = 1   # wall above the alley
    # Alley: rows 9..12 (y 4.5..6.5), cols 20..35 (x 10..17.5), closed at col 36.
    return Scenario(
        name="reverse_uturn",
        grid=g.tolist(),
        resolution=res,
        start=(15.0, 5.5, 0.0),       # deep in the alley, facing the dead end
        goal=(4.0, 5.0, math.pi),     # open room, facing back out
        description="dead-end alley too narrow for a forward U-turn; "
        "the vehicle must reverse out and turn in the open room",
    )


def unsolvable() -> Scenario:
    """Goal boxed in by obstacles: no path exists."""
    res = 0.5
    g = _blank(30, 30)  # 15 m x 15 m
    _add_boundary(g)
    # Closed box around (10, 10): walls at x/y = 8 and 12 (cols/rows 16,24).
    # Interior is 4 m wide, so the goal pose itself is collision-free but
    # unreachable from outside.
    for i in range(16, 25):
        g[16, i] = 1
        g[24, i] = 1
        g[i, 16] = 1
        g[i, 24] = 1
    return Scenario(
        name="unsolvable",
        grid=g.tolist(),
        resolution=res,
        start=(3.0, 3.0, 0.0),
        goal=(10.0, 10.0, 0.0),
        description="goal inside a closed obstacle box; planner must report failure",
    )
