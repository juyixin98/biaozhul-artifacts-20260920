"""Exact Euclidean distance transform (EDT) on 2D occupancy grids.

Algorithm: Felzenszwalb & Huttenlocher separable squared-distance transform
(two 1D passes), extended to

  * carry nearest-obstacle source labels through both passes,
  * support non-square cells (sx != sy), and
  * resolve ties deterministically.

Exactness notes
---------------
All squared distances are computed in *exact rational arithmetic*
(``fractions.Fraction``).  Float cell sizes are dyadic rationals, so every
``((i - q) * sx) ** 2 + ((j - r) * sy) ** 2`` is computed without any
rounding; the only rounding in the whole pipeline is the final conversion
of the exact squared distance to the nearest ``float`` (followed by
``sqrt``).  Envelope intersections are therefore exact too, and a parabola
is popped only when its interval is *provably* empty -- parabolas tying at
a single point survive with a zero-width interval.  The brute-force
reference in ``brute_force.py`` evaluates the identical exact expressions,
so the two implementations agree exactly on distances and sources.

Tie-break rule
--------------
When several obstacle cells are at exactly the minimal distance, the one
with the smallest ``(row, col)`` index (row-major order) is reported as the
source.  The column pass keeps the uppermost obstacle on vertical ties; the
row pass compares tied parabolas by label.
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import List, Optional, Sequence, Tuple

import numpy as np

INF = math.inf

Source = Optional[Tuple[int, int]]  # (col, row) of nearest obstacle, or None


def _column_pass(
    occupied: np.ndarray, sy_ex: Fraction
) -> Tuple[np.ndarray, np.ndarray]:
    """Nearest in-column obstacle per cell.

    Returns ``(dist2, src_row)`` where ``dist2[y, x]`` is the exact squared
    *vertical* distance (a ``Fraction``, or float ``inf``) from cell
    ``(y, x)`` to the nearest occupied cell in column ``x`` and
    ``src_row[y, x]`` is that obstacle's row.  Vertical ties resolve to the
    smaller row index (strict ``<`` on the backward pass).  Columns without
    obstacles keep ``dist2 = inf`` and ``src_row = -1``.
    """
    h, w = occupied.shape
    dist2 = np.full((h, w), INF, dtype=object)
    src_row = np.full((h, w), -1, dtype=np.int64)
    for x in range(w):
        col = occupied[:, x]
        # Forward sweep: nearest occupied row at or above y.
        best = -1
        for y in range(h):
            if col[y]:
                best = y
            if best >= 0:
                dist2[y, x] = (sy_ex * (y - best)) ** 2
                src_row[y, x] = best
        # Backward sweep: nearest occupied row at or below y; strictly
        # smaller distance wins, so ties keep the upper obstacle.
        best = -1
        for y in range(h - 1, -1, -1):
            if col[y]:
                best = y
            if best >= 0:
                d2 = (sy_ex * (best - y)) ** 2
                if d2 < dist2[y, x]:
                    dist2[y, x] = d2
                    src_row[y, x] = best
    return dist2, src_row


def _dt1d(
    f: np.ndarray,
    labels: np.ndarray,
    sx_ex: Fraction,
    out_d2: np.ndarray,
    out_col: np.ndarray,
) -> None:
    """1D exact squared distance transform with source labels (in place).

    ``f[q]`` is the exact value of the parabola rooted at column ``q``
    (``inf`` = column contributes no parabola); ``labels[q]`` is the source
    row attached to that parabola.  Fills ``out_d2[i]`` with the exact
    minimum of ``((i - q) * sx) ** 2 + f[q]`` over all q, and ``out_col[i]``
    with the winning column.  Ties resolve to the smallest
    ``(labels[q], q)``.
    """
    n = f.shape[0]
    sites = [q for q in range(n) if f[q] != INF]
    if not sites:
        return  # out_d2 stays inf, out_col stays -1

    # Lower envelope of parabolas.  v[j] is a site index; its interval is
    # [z[j], z[j+1]).  All arithmetic is exact, so a parabola is popped only
    # when its interval is provably empty; parabolas tying at a single point
    # survive with a zero-width interval and remain available below.
    v: List[int] = []
    z: List = []  # Fractions, with float -inf / +inf sentinels
    for q in sites:
        while True:
            if not v:
                v.append(q)
                z.append(-INF)
                break
            p = v[-1]
            s = ((f[q] + (sx_ex * q) ** 2) - (f[p] + (sx_ex * p) ** 2)) / (
                2 * (sx_ex * q - sx_ex * p)
            )
            if s < z[-1]:
                v.pop()
                z.pop()
                continue
            v.append(q)
            z.append(s)
            break
    z.append(INF)

    def value(j: int, i: int) -> Fraction:
        q = v[j]
        return (sx_ex * (i - q)) ** 2 + f[q]

    def label(j: int) -> Tuple[int, int]:
        q = v[j]
        return (int(labels[q]), q)

    k = 0
    for i in range(n):
        xi = sx_ex * i
        while z[k + 1] < xi:
            k += 1
        # Tie resolution: parabolas tying at i form a run of zero-width
        # intervals around k; scan outward while values keep tying and pick
        # the smallest label.  All comparisons are exact.
        best_j = k
        best_v = value(k, i)
        j = k + 1
        while j < len(v) and value(j, i) <= best_v:
            if value(j, i) < best_v or label(j) < label(best_j):
                best_j, best_v = j, value(j, i)
            j += 1
        j = k - 1
        while j >= 0 and value(j, i) <= best_v:
            if value(j, i) < best_v or label(j) < label(best_j):
                best_j, best_v = j, value(j, i)
            j -= 1
        out_d2[i] = best_v
        out_col[i] = v[best_j]


def _row_pass(
    col_d2: np.ndarray, src_row: np.ndarray, sx_ex: Fraction
) -> Tuple[np.ndarray, np.ndarray]:
    """Apply the 1D transform along every row of the column-pass result."""
    h, w = col_d2.shape
    dist2 = np.full((h, w), INF, dtype=object)
    src_col = np.full((h, w), -1, dtype=np.int64)
    for y in range(h):
        _dt1d(col_d2[y], src_row[y], sx_ex, dist2[y], src_col[y])
    return dist2, src_col


def distance_field(
    occupied: Sequence[Sequence[bool]],
    cell_size: Tuple[float, float] = (1.0, 1.0),
) -> Tuple[np.ndarray, List[List[Source]]]:
    """Compute the exact Euclidean distance field of an occupancy grid.

    Parameters
    ----------
    occupied:
        2D array-like of shape ``(height, width)``; truthy cells are
        obstacles.  ``occupied[y][x]`` indexes row ``y``, column ``x``.
    cell_size:
        ``(sx, sy)`` physical size of one cell; both must be positive and
        finite.  Non-square cells are supported.

    Returns
    -------
    dist:
        ``float64`` array of shape ``(height, width)``; ``dist[y, x]`` is the
        Euclidean distance from the centre of cell ``(y, x)`` to the centre
        of the nearest obstacle cell.  Squared distances are computed in
        exact rational arithmetic and rounded once to the nearest double
        before ``sqrt``.  ``inf`` when the map has no obstacle at all.
    sources:
        Nested lists of the same shape; ``sources[y][x]`` is the ``(col,
        row)`` tuple of the nearest obstacle cell, or ``None`` when the map
        has no obstacle.  Equidistant obstacles resolve to the smallest
        ``(row, col)``.
    """
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

    col_d2, src_row = _column_pass(occ, sy_ex)
    dist2, src_col = _row_pass(col_d2, src_row, sx_ex)

    h, w = occ.shape
    dist = np.empty((h, w), dtype=np.float64)
    sources: List[List[Source]] = []
    for y in range(h):
        row_out: List[Source] = []
        for x in range(w):
            d2 = dist2[y, x]
            if isinstance(d2, Fraction):
                dist[y, x] = math.sqrt(float(d2))
            else:
                dist[y, x] = INF
            q = int(src_col[y, x])
            row_out.append(None if q < 0 else (q, int(src_row[y, q])))
        sources.append(row_out)
    return dist, sources
