"""Core motion-deskew (distortion correction) for 2D laser scans.

Problem
-------
A rotating 2D laser scanner measures each point at a *different* time while
the robot moves. Treating the whole scan as if it were captured at one
instant (the naive "rigid scan" assumption) smears the geometry. Deskewing
re-expresses every point in a single reference frame.

Frames and transform chain
--------------------------
- ``world``: fixed map/world frame.
- ``body``: robot base frame; its motion is given by the pose trajectory
  ``T_world_body(t)``.
- ``laser``: scanner frame, rigidly mounted on the body with constant
  extrinsic ``T_body_laser`` (the sensor extrinsic, body <- laser).

A point measured at time ``t_i`` with polar reading ``(angle_i, range_i)``:

1. laser frame:        p_l   = range_i * [cos(angle_i), sin(angle_i)]
2. world frame:        p_w   = T_world_body(t_i) @ T_body_laser @ p_l
3. reference laser frame at ``t_ref``:

   p_ref = T_body_laser^-1 @ T_world_body(t_ref)^-1 @ p_w

The output points are expressed in the **laser frame as it sits at the
reference time** (equivalently: the scan a static scanner at the reference
pose would have seen). ``t_ref`` itself must also be covered by the pose
trajectory.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .interpolation import PoseCoverageError, interpolate_pose
from .transforms import apply, se2, se2_inv

__all__ = ["PoseCoverageError", "DeskewResult", "deskew_scan"]


@dataclass
class DeskewResult:
    """Result of deskewing one scan."""

    reference_time: float
    points_xy: np.ndarray  # (N, 2) in the reference-time laser frame


def deskew_scan(
    point_times: np.ndarray,
    angles: np.ndarray,
    ranges: np.ndarray,
    pose_times: np.ndarray,
    pose_xytheta: np.ndarray,
    reference_time: float,
    extrinsic_xytheta: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> DeskewResult:
    """Deskew one scan into the laser frame at ``reference_time``.

    Parameters
    ----------
    point_times, angles, ranges:
        (N,) per-point sample time, beam angle (rad, in the laser frame)
        and measured range (m).
    pose_times, pose_xytheta:
        (M,) and (M, 3) body pose trajectory ``T_world_body`` samples.
    reference_time:
        Time whose laser frame all points are expressed in. Must be covered
        by the pose trajectory.
    extrinsic_xytheta:
        (x, y, theta) of the laser frame in the body frame
        (``T_body_laser``).

    Returns
    -------
    DeskewResult with (N, 2) corrected points.

    Raises
    ------
    PoseCoverageError
        If any point time or the reference time is not covered by the pose
        trajectory.
    """
    point_times = np.asarray(point_times, dtype=float)
    angles = np.asarray(angles, dtype=float)
    ranges = np.asarray(ranges, dtype=float)
    pose_xytheta = np.asarray(pose_xytheta, dtype=float)
    if not (point_times.shape == angles.shape == ranges.shape):
        raise ValueError("point_times, angles and ranges must have equal length")

    # Pose of the body at every point time and at the reference time.
    # interpolate_pose raises PoseCoverageError on any uncovered time.
    query = np.concatenate([point_times, [reference_time]])
    interp = interpolate_pose(np.asarray(pose_times, dtype=float), pose_xytheta, query)
    body_at_points = interp[:-1]
    ref_pose = interp[-1]

    t_body_laser = se2(*extrinsic_xytheta)
    t_laser_body = se2_inv(t_body_laser)
    t_ref_inv = se2_inv(se2(*ref_pose))  # T_body(ref)_world

    # Points in their own laser frame, then into the world.
    p_laser = np.column_stack([ranges * np.cos(angles), ranges * np.sin(angles)])
    corrected = np.empty_like(p_laser)
    for i, (x, y, theta) in enumerate(body_at_points):
        t_world_laser_i = se2(x, y, theta) @ t_body_laser
        # Chain: laser_i -> world -> body(ref) -> laser_ref
        t_ref_laser_i = t_laser_body @ t_ref_inv @ t_world_laser_i
        corrected[i] = apply(t_ref_laser_i, p_laser[i : i + 1])[0]

    return DeskewResult(reference_time=float(reference_time), points_xy=corrected)
