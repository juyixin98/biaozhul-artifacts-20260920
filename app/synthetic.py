"""Synthetic data generation: a moving robot scanning a straight wall.

Used by the tests and the example script. No real hardware involved.

Scene
-----
A wall is the vertical line ``x = wall_x`` in the world frame. The robot
moves with constant linear velocity ``v = (vx, vy)`` and constant angular
velocity ``omega`` starting from pose ``(x0, y0, theta0)`` at ``t0``. The
scanner rotates at a fixed rate, emitting beams between ``angle_min`` and
``angle_max`` (in the laser frame) over the scan duration.

For each beam we intersect the ray with the wall analytically, giving the
ground-truth range. The *distorted* (naive) scan treats every point as if
captured at the reference pose; the deskewed scan should recover the true
wall.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class SyntheticScan:
    point_times: np.ndarray  # (N,)
    angles: np.ndarray  # (N,) beam angle in laser frame
    ranges: np.ndarray  # (N,) ground-truth ranges
    pose_times: np.ndarray  # (M,)
    pose_xytheta: np.ndarray  # (M, 3) ground-truth body poses
    reference_time: float
    extrinsic: tuple[float, float, float]
    wall_x: float


def constant_velocity_pose(
    t: np.ndarray | float,
    t0: float,
    x0: float,
    y0: float,
    theta0: float,
    vx: float,
    vy: float,
    omega: float,
) -> np.ndarray:
    """Ground-truth pose (x, y, theta) under constant linear/angular velocity."""
    t = np.asarray(t, dtype=float)
    dt = t - t0
    return np.stack(
        [x0 + vx * dt, y0 + vy * dt, theta0 + omega * dt], axis=-1
    )


def simulate_wall_scan(
    wall_x: float = 5.0,
    t0: float = 0.0,
    duration: float = 0.1,
    n_points: int = 360,
    n_pose_samples: int = 21,
    x0: float = 0.0,
    y0: float = 0.0,
    theta0: float = 0.0,
    vx: float = 0.5,
    vy: float = 0.1,
    omega: float = 0.8,
    angle_min: float = -0.6,
    angle_max: float = 0.6,
    extrinsic: tuple[float, float, float] = (0.1, 0.0, 0.0),
    reference_time: float | None = None,
    range_noise_std: float = 0.0,
    seed: int = 0,
) -> SyntheticScan:
    """Simulate one scan of a straight wall by a constantly-moving robot.

    Only beams that actually hit the wall (forward-facing fan) are kept, so
    every returned point has a finite ground-truth range.
    """
    if reference_time is None:
        reference_time = t0 + duration / 2.0

    ex, ey, etheta = extrinsic
    point_times = np.linspace(t0, t0 + duration, n_points)
    angles = np.linspace(angle_min, angle_max, n_points)

    body_pose = constant_velocity_pose(
        point_times, t0, x0, y0, theta0, vx, vy, omega
    )
    # Laser origin and beam direction in the world frame.
    laser_theta = body_pose[:, 2] + etheta
    laser_x = body_pose[:, 0] + ex * np.cos(body_pose[:, 2]) - ey * np.sin(body_pose[:, 2])
    laser_y = body_pose[:, 1] + ex * np.sin(body_pose[:, 2]) + ey * np.cos(body_pose[:, 2])
    beam_dir = laser_theta + angles

    # Ray (laser_x, laser_y) + s*(cos, sin) hits wall x = wall_x.
    cos_dir = np.cos(beam_dir)
    ranges = (wall_x - laser_x) / cos_dir
    hit = (ranges > 0) & np.isfinite(ranges)

    point_times, angles, ranges = point_times[hit], angles[hit], ranges[hit]
    if range_noise_std > 0:
        rng = np.random.default_rng(seed)
        ranges = ranges + rng.normal(0.0, range_noise_std, size=ranges.shape)

    pose_times = np.linspace(t0, t0 + duration, n_pose_samples)
    pose_xytheta = constant_velocity_pose(
        pose_times, t0, x0, y0, theta0, vx, vy, omega
    )

    return SyntheticScan(
        point_times=point_times,
        angles=angles,
        ranges=ranges,
        pose_times=pose_times,
        pose_xytheta=pose_xytheta,
        reference_time=reference_time,
        extrinsic=extrinsic,
        wall_x=wall_x,
    )


def wall_residuals(points_xy: np.ndarray, wall_x: float) -> np.ndarray:
    """Signed distance of points (in the reference laser frame) to the wall.

    The wall position in the reference laser frame is computed by the caller
    for the general case; here we assume the caller passes points already
    expressed in a frame where the wall is ``x = wall_x`` — see
    ``points_in_world`` helper below.
    """
    return points_xy[:, 0] - wall_x
