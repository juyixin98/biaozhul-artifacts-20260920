"""Brute-force reference implementation of the Euclidean distance field.

O(height * width * #obstacles).  Used to validate the fast separable
transform in ``distance_field.py``; also perfectly usable on its own for
small maps.

Squared distances are computed in exact rational arithmetic
(``fractions.Fraction``) -- the identical expressions used by the fast
transform -- so the two implementations agree exactly.

Tie-break rule (shared with ``distance_field``): obstacles are scanned in
row-major order and a strictly smaller distance is required to replace the
incumbent, so among equidistant obstacles the smallest ``(row, col)`` wins.
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import List, Optional, Sequence, Tuple

import numpy as np

INF = math.inf

Source = Optional[Tuple[int, int]]


def brute_force_field(
    occupied: Sequence[Sequence[bool]],
    cell_size: Tuple[float, float] = (1.0, 1.0),
) -> Tuple[np.ndarray, List[List[Source]]]:
    """Reference distance field; same contract as ``distance_field``."""
    occ = np.asarray(occupied, dtype=bool)
    if occ.ndim != 2:
        raise ValueError(f"occupied must be 2D, got ndim={occ.ndim}")
    sx, sy = float(cell_size[0]), float(cell_size[1])
    if not (math.isfinite(sx) and sx > 0.0):
        raise ValueError(f"cell_size[0] must be positive and finite, got {sx!r}")
    if not (math.isfinite(sy) and sy > 0.0):
        raise ValueError(f"cell_size[1] must be positive and finite, got {sy!r}")
    sx_ex = Fraction(sx)
    sy_ex = Fraction(sy)

    h, w = occ.shape
    obstacles = [(x, y) for y in range(h) for x in range(w) if occ[y, x]]

    dist = np.full((h, w), INF, dtype=np.float64)
    sources: List[List[Source]] = [[None] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            best_d2: Optional[Fraction] = None
            best_src: Source = None
            for ox, oy in obstacles:  # row-major: first wins ties
                d2 = (sx_ex * (x - ox)) ** 2 + (sy_ex * (y - oy)) ** 2
                if best_d2 is None or d2 < best_d2:
                    best_d2 = d2
                    best_src = (ox, oy)
            if best_d2 is not None:
                dist[y, x] = math.sqrt(float(best_d2))
            sources[y][x] = best_src
    return dist, sources
