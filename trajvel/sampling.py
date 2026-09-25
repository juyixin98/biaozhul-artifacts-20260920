"""Sampling of a time-parameterized polyline trajectory.

Given a ParameterizationResult, evaluate position, scalar speed and signed
tangential acceleration at a fixed time step. Useful for offline validation
(e.g. checking the velocity/acceleration bounds numerically) — no hardware,
no visualization.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .geometry import EPS, segment_lengths, unit_directions
from .parameterize import ParameterizationResult


@dataclass(frozen=True)
class SegmentProfile:
    """Trapezoidal/triangular speed profile of one segment."""

    distance: float
    v0: float
    v1: float
    accel: float  # signed magnitude used while speeding up
    t_accel: float  # end of the acceleration phase
    t_cruise: float  # end of the cruise phase (== t_accel for triangular)
    duration: float
    v_peak: float

    def evaluate(self, tau: float) -> tuple[float, float, float]:
        """Return (arc position, speed, signed acceleration) at local time tau."""
        tau = min(max(tau, 0.0), self.duration)
        if tau <= self.t_accel:
            speed = self.v0 + self.accel * tau
            arc = self.v0 * tau + 0.5 * self.accel * tau * tau
            return arc, speed, self.accel
        s_accel = self.v0 * self.t_accel + 0.5 * self.accel * self.t_accel**2
        if tau <= self.t_cruise:
            return s_accel + self.v_peak * (tau - self.t_accel), self.v_peak, 0.0
        remaining = self.duration - tau
        speed = self.v1 + self.accel * remaining
        arc = self.distance - (self.v1 * remaining + 0.5 * self.accel * remaining**2)
        return arc, speed, -self.accel


def build_segment_profile(
    distance: float,
    v0: float,
    v1: float,
    max_acceleration: float,
    max_velocity: float,
) -> SegmentProfile:
    """Build the minimum-time speed profile for one segment.

    Mirrors trajvel.parameterize.segment_duration; zero-length segments
    produce a zero-duration profile (never evaluated, never divided by).
    """
    if distance <= EPS:
        return SegmentProfile(distance, v0, v1, max_acceleration, 0.0, 0.0, 0.0, max(v0, v1))
    a = max_acceleration
    v_peak = math.sqrt(max((2.0 * a * distance + v0 * v0 + v1 * v1) / 2.0, 0.0))
    if v_peak > max_velocity:
        v_peak = max_velocity
    t_accel = (v_peak - v0) / a
    t_decel = (v_peak - v1) / a
    s_accel = (v_peak**2 - v0**2) / (2.0 * a)
    s_decel = (v_peak**2 - v1**2) / (2.0 * a)
    s_cruise = max(distance - s_accel - s_decel, 0.0)
    t_cruise_end = t_accel + s_cruise / v_peak if v_peak > EPS else t_accel
    duration = t_cruise_end + t_decel
    return SegmentProfile(distance, v0, v1, a, t_accel, t_cruise_end, duration, v_peak)


def sample_trajectory(
    result: ParameterizationResult,
    max_acceleration: float,
    max_velocity: float,
    dt: float = 0.01,
) -> dict[str, np.ndarray]:
    """Sample the trajectory at a fixed time step.

    Returns a dict of arrays: ``t`` (M,), ``positions`` (M, D),
    ``speeds`` (M,), ``accelerations`` (M,) signed tangential acceleration,
    and ``velocities`` (M, D) Cartesian velocity vectors.
    """
    if dt <= 0.0:
        raise ValueError(f"dt must be positive, got {dt}")
    points = result.points
    lengths = segment_lengths(points)
    directions = unit_directions(points, lengths)
    profiles = [
        build_segment_profile(
            lengths[i],
            result.node_velocities[i],
            result.node_velocities[i + 1],
            max_acceleration,
            max_velocity,
        )
        for i in range(len(lengths))
    ]

    total = result.total_time
    n_samples = int(math.floor(total / dt)) + 1
    times = np.minimum(np.arange(n_samples) * dt, total)

    positions = np.zeros((n_samples, points.shape[1]))
    velocities = np.zeros_like(positions)
    speeds = np.zeros(n_samples)
    accelerations = np.zeros(n_samples)

    segment_index = 0
    for k, t in enumerate(times):
        while (
            segment_index < len(profiles) - 1
            and t > result.waypoint_times[segment_index + 1]
        ):
            segment_index += 1
        profile = profiles[segment_index]
        if profile.duration <= EPS:
            positions[k] = points[segment_index + 1]
            continue
        tau = t - result.waypoint_times[segment_index]
        arc, speed, accel = profile.evaluate(tau)
        arc = min(arc, lengths[segment_index])
        direction = directions[segment_index]
        positions[k] = points[segment_index] + direction * arc
        velocities[k] = direction * speed
        speeds[k] = speed
        accelerations[k] = accel

    return {
        "t": times,
        "positions": positions,
        "velocities": velocities,
        "speeds": speeds,
        "accelerations": accelerations,
    }
