"""Synthetic data: a robot scanning a straight wall while moving.

The world contains a single vertical wall ``x = wall_x``. The body moves
with constant linear velocity ``velocity = (vx, vy)`` (world frame) and
constant yaw rate ``omega``, starting from ``pose0`` at ``t = 0``. A
2D laser rigidly mounted with extrinsic ``T_B_L`` fires beams at angles
``angle_min..angle_max`` (laser frame), one beam per sample time in
``[t_scan_start, t_scan_end]``. Each beam is ray-cast against the wall,
giving the ground-truth range at that instant.

Because the true motion is linear in time, the linear/Slerp pose
interpolation in :mod:`app.deskew` is exact and a perfect deskew recovers
the wall up to floating-point error.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .deskew import se2_matrices


@dataclass
class SyntheticScan:
    wall_x: float
    ranges: np.ndarray          # (M,) ground-truth ranges
    angles: np.ndarray          # (M,) beam angles in the laser frame
    point_times: np.ndarray     # (M,) sampling time per beam
    pose_times: np.ndarray      # (K,) pose sequence times
    poses: np.ndarray           # (K, 3) ground-truth T_W_B poses
    reference_time: float
    pose0: tuple[float, float, float]
    extrinsic: tuple[float, float, float]
    velocity: tuple[float, float]
    omega: float


def true_pose(t, pose0, velocity, omega) -> np.ndarray:
    """Constant-velocity ground-truth pose T_W_B at times ``t``."""
    t = np.asarray(t, dtype=float)
    x0, y0, th0 = pose0
    vx, vy = velocity
    return np.column_stack([x0 + vx * t, y0 + vy * t, th0 + omega * t])


def simulate_wall_scan(
    wall_x: float = 5.0,
    n_beams: int = 361,
    angle_min: float = np.deg2rad(-60.0),
    angle_max: float = np.deg2rad(60.0),
    t_scan_start: float = 0.0,
    t_scan_end: float = 0.1,
    pose0: tuple[float, float, float] = (0.0, 0.0, 0.0),
    velocity: tuple[float, float] = (1.0, 0.3),
    omega: float = 0.5,
    extrinsic: tuple[float, float, float] = (0.10, 0.02, 0.0),
    pose_rate_hz: float = 100.0,
    reference_time: float | None = None,
) -> SyntheticScan:
    """Simulate one distorted scan of a straight wall.

    Returns ground-truth ranges (as the moving sensor would measure them),
    the true pose sequence, and everything needed to deskew the scan.
    """
    if reference_time is None:
        reference_time = t_scan_start

    point_times = np.linspace(t_scan_start, t_scan_end, n_beams)
    angles = np.linspace(angle_min, angle_max, n_beams)

    # Pose sequence spanning the scan with margin, at pose_rate_hz.
    n_poses = int(np.ceil((t_scan_end - t_scan_start) * pose_rate_hz)) + 3
    pose_times = np.linspace(
        t_scan_start - 0.5 / pose_rate_hz, t_scan_end + 0.5 / pose_rate_hz, n_poses
    )
    poses = true_pose(pose_times, pose0, velocity, omega)

    # Laser pose in the world at each beam time: T_W_L = T_W_B @ T_B_L.
    T_B_L = se2_matrices(np.asarray(extrinsic, dtype=float).reshape(1, 3))[0]
    T_W_L = se2_matrices(true_pose(point_times, pose0, velocity, omega)) @ T_B_L
    origin = T_W_L[:, :2, 2]                       # (M, 2)
    yaw = np.arctan2(T_W_L[:, 1, 0], T_W_L[:, 0, 0])
    direction = np.column_stack(
        [np.cos(yaw + angles), np.sin(yaw + angles)]
    )                                            # (M, 2)

    # Ray-cast against the wall plane x = wall_x.
    denom = direction[:, 0]
    if np.any(np.abs(denom) < 1e-9):
        raise ValueError("a beam is parallel to the wall; adjust the scenario")
    ranges = (wall_x - origin[:, 0]) / denom
    if np.any(ranges <= 0):
        raise ValueError("a beam points away from the wall; adjust the scenario")

    return SyntheticScan(
        wall_x=wall_x,
        ranges=ranges,
        angles=angles,
        point_times=point_times,
        pose_times=pose_times,
        poses=poses,
        reference_time=reference_time,
        pose0=pose0,
        extrinsic=extrinsic,
        velocity=velocity,
        omega=omega,
    )


def naive_scan_points(scan: SyntheticScan) -> np.ndarray:
    """The distorted cloud: raw ranges/angles placed as if the sensor never
    moved (what naive mapping without deskew would use)."""
    return np.column_stack(
        [scan.ranges * np.cos(scan.angles), scan.ranges * np.sin(scan.angles)]
    )


def wall_rmse_in_world(points_laser_ref: np.ndarray, scan: SyntheticScan) -> float:
    """RMSE of distance to the wall, with points mapped back to the world.

    ``points_laser_ref`` are Cartesian points expressed in the laser frame
    at ``scan.reference_time`` (the deskewed output convention, and also
    the implicit convention of the naive distorted cloud). They are mapped
    to the world with the true reference pose; for a perfect scan of the
    wall every point then has ``x == wall_x``.
    """
    T_B_L = se2_matrices(np.asarray(scan.extrinsic).reshape(1, 3))[0]
    T_W_B_ref = se2_matrices(
        true_pose(
            np.array([scan.reference_time]), scan.pose0, scan.velocity, scan.omega
        )
    )[0]
    T_W_L_ref = T_W_B_ref @ T_B_L
    pts = np.asarray(points_laser_ref, dtype=float)
    hom = np.column_stack([pts, np.ones(len(pts))])
    p_W = (T_W_L_ref @ hom.T).T[:, :2]
    return float(np.sqrt(np.mean((p_W[:, 0] - scan.wall_x) ** 2)))
