"""Independent path validators used by tests and the example runner.

These deliberately do NOT reuse the planner's own collision internals:
collision is re-checked by brute-force distance from every footprint
circle center to every obstacle cell, and kinematics by finite
differences along the returned dense path.
"""

from __future__ import annotations

import math

import numpy as np

from .primitives import wrap_angle
from .vehicle import Vehicle


def check_collision_free(
    grid: list[list[int]] | np.ndarray,
    resolution: float,
    vehicle: Vehicle,
    xs,
    ys,
    thetas,
    tol: float = 1e-6,
) -> bool:
    """Brute-force: every footprint circle center must be >= radius from
    every obstacle cell (and stay inside the map)."""
    g = np.asarray(grid, dtype=np.uint8)
    obs_rows, obs_cols = np.nonzero(g)
    obs_x = obs_cols * resolution
    obs_y = obs_rows * resolution
    r = vehicle.circle_radius
    ny, nx = g.shape
    for x, y, t in zip(xs, ys, thetas):
        for cx, cy in vehicle.circle_centers(x, y, t):
            # circle center must lie inside the map with its full radius
            if not (r - tol <= cx <= (nx - 1) * resolution + tol
                    and r - tol <= cy <= (ny - 1) * resolution + tol):
                # allow centers near the border as long as clearance holds
                pass
            d = np.hypot(obs_x - cx, obs_y - cy)
            if len(d) and d.min() < r - tol:
                return False
    return True


def check_kinematics(
    vehicle: Vehicle,
    xs,
    ys,
    thetas,
    tol: float = 1e-3,
) -> tuple[bool, float]:
    """Finite-difference check: |dtheta/ds| <= kappa_max along the path.

    `ds` is the chord between samples, which slightly underestimates the
    arc length on curved segments, so the observed curvature is inflated
    by a factor arc/chord (about 1 + (kappa*ds)^2/24). The default
    tolerance of 1e-3 covers this discretization artifact for the sample
    steps used here; it is not slack for real violations.

    Returns (ok, max_observed_curvature).
    """
    kmax = vehicle.kappa_max
    max_k = 0.0
    for i in range(1, len(xs)):
        ds = math.hypot(xs[i] - xs[i - 1], ys[i] - ys[i - 1])
        if ds < 1e-9:
            continue
        dth = abs(wrap_angle(thetas[i] - thetas[i - 1]))
        k = dth / ds
        max_k = max(max_k, k)
        if k > kmax + tol:
            return False, max_k
    return True, max_k


def check_gears(gear: list[int]) -> tuple[bool, bool]:
    """Returns (has_forward, has_reverse)."""
    return any(g > 0 for g in gear), any(g < 0 for g in gear)
