"""Degeneracy / uncertainty analysis for point-to-point 2D ICP.

Why a dedicated module: for point-to-point ICP the Gauss-Newton Hessian is
(almost) always full rank, because every correspondence constrains both
coordinates of the transformed point. The classic "corridor" degeneracy --
translation along a straight wall is unobservable -- therefore does NOT show
up as a singular Hessian. It shows up in the *cost landscape* instead: after
nearest-neighbour re-association, sliding the source along the wall leaves
the residual nearly unchanged.

This module probes the cost landscape empirically. Around the final pose it
evaluates a fixed-trim alignment cost (the mean of the smallest 90% of
squared nearest-neighbour distances -- a fixed trim keeps the landscape
well-defined, unlike an adaptive gate which re-fits at every probe) for
sweeps of increasing offsets along many directions. Directions whose cost
increase is a tiny fraction of the stiffest direction are reported as flat,
i.e. the estimate is uncertain along them.

Two probe-design notes, learned from experiments:

- A hard distance gate makes the cost flat in *every* direction once points
  are rejected, so the probe uses trimming with a fixed ratio instead.
- Scan discretization creates a periodic local landscape along a wall
  (period = sample spacing), so probing only at tiny offsets measures the
  local well, not the global flatness. The sweep up to 1 m captures the
  envelope.

The analytic Hessian eigenvalue ratio is reported alongside as a secondary
signal only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .geometry import transform_points
from .kdtree import nearest_neighbors

_TRANSLATION_SWEEPS_M = (0.25, 0.5, 1.0)
_ROTATION_SWEEPS_RAD = (0.1, 0.3)
_NUM_TRANSLATION_DIRECTIONS = 12
_PROBE_TRIM_RATIO = 0.9


@dataclass
class DegeneracyReport:
    """Uncertainty assessment attached to every ICP result."""

    degenerate: bool
    flat_translation_direction: list[float] | None  # unit vector or None
    rotation_flat: bool
    min_translation_cost_increase: float
    max_translation_cost_increase: float
    translation_flatness_ratio: float  # min / max directional cost increase
    rotation_cost_increase: float
    rotation_flatness_ratio: float  # rotation increase / max translation increase
    hessian_eigenvalue_ratio: float  # lambda_max / lambda_min of GN Hessian
    message: str
    details: dict = field(default_factory=dict)


def analyze_degeneracy(
    source: np.ndarray,
    target: np.ndarray,
    pose: np.ndarray,
    config,
    hessian_eigenvalues: np.ndarray,
) -> DegeneracyReport:
    """Probe the cost landscape around ``pose`` for flat (unobservable) axes."""
    base_cost = _probe_cost(source, target, pose)

    angles = np.linspace(0.0, np.pi, _NUM_TRANSLATION_DIRECTIONS, endpoint=False)
    increases = np.array(
        [
            _directional_cost_increase(source, target, pose, angle, base_cost)
            for angle in angles
        ]
    )
    rotation_increase = _rotation_cost_increase(source, target, pose, base_cost)

    min_inc = float(np.min(increases))
    max_inc = float(np.max(increases))
    trans_ratio = min_inc / max_inc if max_inc > 0 else 0.0
    rot_ratio = rotation_increase / max_inc if max_inc > 0 else 0.0
    flat_index = int(np.argmin(increases))
    flat_direction = [float(np.cos(angles[flat_index])), float(np.sin(angles[flat_index]))]

    threshold = config.degeneracy_flatness_ratio
    degenerate = bool(trans_ratio < threshold)
    rotation_flat = bool(rot_ratio < threshold)
    eig_ratio = _eigenvalue_ratio(hessian_eigenvalues)

    if degenerate:
        message = (
            "degenerate geometry: translation along "
            f"({flat_direction[0]:.3f}, {flat_direction[1]:.3f}) is nearly "
            "unobservable; the pose is uncertain along this direction"
        )
    else:
        message = "no degenerate direction detected"

    return DegeneracyReport(
        degenerate=degenerate,
        flat_translation_direction=flat_direction if degenerate else None,
        rotation_flat=rotation_flat,
        min_translation_cost_increase=min_inc,
        max_translation_cost_increase=max_inc,
        translation_flatness_ratio=trans_ratio,
        rotation_cost_increase=float(rotation_increase),
        rotation_flatness_ratio=float(rot_ratio),
        hessian_eigenvalue_ratio=eig_ratio,
        message=message,
        details={
            "translation_cost_increases": [float(v) for v in increases],
            "probe_angles_rad": [float(a) for a in angles],
            "translation_sweeps_m": list(_TRANSLATION_SWEEPS_M),
            "rotation_sweeps_rad": list(_ROTATION_SWEEPS_RAD),
        },
    )


def _probe_cost(source: np.ndarray, target: np.ndarray, pose: np.ndarray) -> float:
    """Mean of the smallest 90% squared NN distances at ``pose``.

    A fixed trim ratio (rather than the solver's adaptive gate) keeps this
    cost a well-defined function of the pose, which is what the landscape
    probe needs.
    """
    transformed = transform_points(source, pose)
    distances, _ = nearest_neighbors(transformed, target)
    keep = max(1, int(np.ceil(_PROBE_TRIM_RATIO * len(distances))))
    smallest = np.partition(distances, keep - 1)[:keep]
    return float(np.mean(smallest**2))


def _directional_cost_increase(
    source: np.ndarray,
    target: np.ndarray,
    pose: np.ndarray,
    angle: float,
    base_cost: float,
) -> float:
    """Max cost increase when sliding along ``angle`` over the sweep scales."""
    direction = np.array([np.cos(angle), np.sin(angle)])
    best = 0.0
    for scale in _TRANSLATION_SWEEPS_M:
        delta = scale * direction
        for sign in (1.0, -1.0):
            cost = _probe_cost(
                source, target, pose + sign * np.array([delta[0], delta[1], 0.0])
            )
            best = max(best, cost - base_cost)
    return best


def _rotation_cost_increase(
    source: np.ndarray, target: np.ndarray, pose: np.ndarray, base_cost: float
) -> float:
    """Max cost increase when rotating about the pose origin over the sweep."""
    best = 0.0
    for scale in _ROTATION_SWEEPS_RAD:
        for sign in (1.0, -1.0):
            cost = _probe_cost(source, target, pose + sign * np.array([0.0, 0.0, scale]))
            best = max(best, cost - base_cost)
    return best


def _eigenvalue_ratio(eigenvalues: np.ndarray) -> float:
    """lambda_max / lambda_min of the Gauss-Newton Hessian (inf if invalid)."""
    eigenvalues = np.asarray(eigenvalues, dtype=float)
    if eigenvalues.shape != (3,) or not np.all(np.isfinite(eigenvalues)):
        return float("inf")
    smallest = max(float(np.min(np.abs(eigenvalues))), 1e-15)
    return float(np.max(np.abs(eigenvalues)) / smallest)
