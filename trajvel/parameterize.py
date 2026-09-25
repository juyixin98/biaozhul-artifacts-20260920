"""Time parameterization of a polyline under velocity/acceleration bounds.

Algorithm (classic forward/backward pass, as in MoveIt's
iterative_time_parameterization and TOPP-style planners):

1. Node velocity limits: every node is capped at ``max_velocity``; interior
   corner nodes are further capped either by an explicit curvature model
   (``v <= sqrt(a_lat / kappa)``) or forced to a full stop.
2. Forward pass:  v[i+1] = min(limit[i+1], sqrt(v[i]^2 + 2*a*s[i]))
3. Backward pass: v[i]   = min(v[i],       sqrt(v[i+1]^2 + 2*a*s[i]))
4. Each segment is timed with a trapezoidal (or triangular) velocity
   profile between its boundary velocities.

After both passes every segment satisfies
``v[i+1]^2 <= v[i]^2 + 2*a*s[i]`` and vice versa, so the per-segment
profiles are feasible by construction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from .geometry import EPS, dedupe_points, segment_lengths, turning_angles


class CornerStrategy(str, Enum):
    """How to limit speed at interior corner nodes."""

    CURVATURE = "curvature"  # v <= sqrt(a_lat / kappa) from an explicit curvature model
    STOP = "stop"  # come to a full stop at every corner


@dataclass(frozen=True)
class Constraints:
    """Motion limits. All values must be strictly positive."""

    max_velocity: float
    max_acceleration: float
    max_lateral_acceleration: float

    def __post_init__(self) -> None:
        for name in ("max_velocity", "max_acceleration", "max_lateral_acceleration"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be a positive finite number, got {value}")


@dataclass
class ParameterizationResult:
    """Output of :func:`parameterize`."""

    points: np.ndarray  # (N, D) waypoints actually used (after dedupe)
    node_velocities: np.ndarray  # (N,) scalar speed at each node
    segment_durations: np.ndarray  # (N-1,) seconds per segment
    waypoint_times: np.ndarray  # (N,) cumulative time at each node
    removed_duplicate_points: int = 0
    corner_velocities: dict = field(default_factory=dict)  # node index -> cap

    @property
    def total_time(self) -> float:
        return float(self.waypoint_times[-1])


def corner_curvature(points: np.ndarray, lengths: np.ndarray, index: int) -> float:
    """Curvature (1/m) at an interior node via the circumscribed-circle model.

    The polyline corner is approximated by the arc of the circle through the
    previous, current and next points; ``kappa = 2*sin(theta) / chord`` where
    the chord joins the midpoints of the adjacent segments. Returns 0.0 for
    straight nodes and ``inf`` when the adjacent geometry is degenerate
    (zero-length segments), which callers treat as "must stop".
    """
    s_prev = lengths[index - 1]
    s_next = lengths[index]
    if s_prev <= EPS or s_next <= EPS:
        return math.inf
    d0 = (points[index] - points[index - 1]) / s_prev
    d1 = (points[index + 1] - points[index]) / s_next
    cos_angle = float(np.clip(np.dot(d0, d1), -1.0, 1.0))
    sin_angle = math.sqrt(max(0.0, 1.0 - cos_angle * cos_angle))
    if sin_angle <= EPS:
        return 0.0
    # Chord between adjacent segment midpoints; the turning angle of the
    # tangent along the inscribed arc equals the heading change.
    chord = 0.5 * math.sqrt(
        s_prev * s_prev + s_next * s_next + 2.0 * s_prev * s_next * cos_angle
    )
    if chord <= EPS:
        return math.inf
    return sin_angle / chord


def node_velocity_limits(
    points: np.ndarray,
    lengths: np.ndarray,
    constraints: Constraints,
    strategy: CornerStrategy,
) -> tuple[np.ndarray, dict]:
    """Per-node speed caps. Returns (limits, corner_caps) where corner_caps
    maps interior node index -> imposed cap (for reporting)."""
    n = len(points)
    limits = np.full(n, constraints.max_velocity)
    corner_caps: dict[int, float] = {}
    angles = turning_angles(points)
    for i in range(1, n - 1):
        if angles[i] <= 1e-9:
            continue  # straight-through node, no corner limit
        if strategy is CornerStrategy.STOP:
            cap = 0.0
        else:
            kappa = corner_curvature(points, lengths, i)
            if math.isinf(kappa):
                cap = 0.0
            elif kappa <= EPS:
                continue
            else:
                cap = min(
                    constraints.max_velocity,
                    math.sqrt(constraints.max_lateral_acceleration / kappa),
                )
        limits[i] = cap
        corner_caps[i] = cap
    return limits, corner_caps


def forward_backward_pass(
    lengths: np.ndarray,
    limits: np.ndarray,
    max_acceleration: float,
    start_velocity: float,
    end_velocity: float,
) -> np.ndarray:
    """Propagate node velocities forward then backward under accel bounds."""
    velocities = limits.astype(float).copy()
    velocities[0] = min(start_velocity, limits[0])
    velocities[-1] = min(end_velocity, limits[-1])
    for i in range(len(lengths)):
        reachable = math.sqrt(velocities[i] ** 2 + 2.0 * max_acceleration * lengths[i])
        velocities[i + 1] = min(velocities[i + 1], reachable)
    for i in range(len(lengths) - 1, -1, -1):
        reachable = math.sqrt(velocities[i + 1] ** 2 + 2.0 * max_acceleration * lengths[i])
        velocities[i] = min(velocities[i], reachable)
    return velocities


def segment_duration(
    distance: float,
    v0: float,
    v1: float,
    max_acceleration: float,
    max_velocity: float,
) -> float:
    """Minimum time for one segment with a trapezoidal/triangular profile.

    Assumes the feasibility invariant from the forward/backward pass:
    ``v1^2 <= v0^2 + 2*a*distance`` and symmetric. Zero-length segments
    take zero time (no division by the distance occurs).
    """
    if distance <= EPS:
        return 0.0
    a = max_acceleration
    v_peak = math.sqrt(max((2.0 * a * distance + v0 * v0 + v1 * v1) / 2.0, 0.0))
    if v_peak <= max_velocity:
        # Triangular profile: accelerate to v_peak, then decelerate.
        return (2.0 * v_peak - v0 - v1) / a
    # Trapezoidal profile with a cruise phase at max_velocity.
    t_acc = (max_velocity - v0) / a
    t_dec = (max_velocity - v1) / a
    s_acc = (max_velocity**2 - v0**2) / (2.0 * a)
    s_dec = (max_velocity**2 - v1**2) / (2.0 * a)
    s_cruise = max(distance - s_acc - s_dec, 0.0)
    return t_acc + s_cruise / max_velocity + t_dec


def parameterize(
    waypoints: object,
    max_velocity: float,
    max_acceleration: float,
    max_lateral_acceleration: float,
    corner_strategy: str | CornerStrategy = CornerStrategy.CURVATURE,
    start_velocity: float = 0.0,
    end_velocity: float = 0.0,
) -> ParameterizationResult:
    """Time-parameterize a polyline.

    Args:
        waypoints: (N, D) array-like of polyline nodes (N >= 2).
        max_velocity: scalar speed limit (m/s).
        max_acceleration: tangential acceleration limit (m/s^2).
        max_lateral_acceleration: centripetal limit used by the curvature
            corner strategy (m/s^2).
        corner_strategy: "curvature" or "stop".
        start_velocity / end_velocity: boundary speeds, must be within
            [0, max_velocity].

    Returns:
        ParameterizationResult with per-node speeds, per-segment durations
        and cumulative waypoint times.
    """
    from .geometry import as_points

    strategy = CornerStrategy(corner_strategy)
    constraints = Constraints(max_velocity, max_acceleration, max_lateral_acceleration)
    for name, value in (("start_velocity", start_velocity), ("end_velocity", end_velocity)):
        if not (0.0 <= value <= max_velocity):
            raise ValueError(f"{name} must be in [0, max_velocity], got {value}")

    raw = as_points(waypoints)
    points, removed = dedupe_points(raw)
    lengths = segment_lengths(points)

    limits, corner_caps = node_velocity_limits(points, lengths, constraints, strategy)
    velocities = forward_backward_pass(
        lengths, limits, max_acceleration, start_velocity, end_velocity
    )
    durations = np.array(
        [
            segment_duration(s, velocities[i], velocities[i + 1], max_acceleration, max_velocity)
            for i, s in enumerate(lengths)
        ]
    )
    waypoint_times = np.concatenate(([0.0], np.cumsum(durations)))
    return ParameterizationResult(
        points=points,
        node_velocities=velocities,
        segment_durations=durations,
        waypoint_times=waypoint_times,
        removed_duplicate_points=removed,
        corner_velocities=corner_caps,
    )
