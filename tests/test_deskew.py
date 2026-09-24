"""End-to-end deskew validation on a synthetic straight wall."""

import numpy as np
import pytest

from app.deskew import PoseCoverageError, deskew_scan
from app.interpolation import interpolate_pose
from app.synthetic import simulate_wall_scan
from app.transforms import apply, se2


def _reference_world_transform(scan) -> np.ndarray:
    """T_world_laser at the reference time (from the pose trajectory)."""
    ref_pose = interpolate_pose(
        scan.pose_times, scan.pose_xytheta, np.array([scan.reference_time])
    )[0]
    return se2(*ref_pose) @ se2(*scan.extrinsic)


def _naive_points_world(scan) -> np.ndarray:
    """Distorted baseline: every point treated as captured at the reference pose."""
    p_laser = np.column_stack(
        [scan.ranges * np.cos(scan.angles), scan.ranges * np.sin(scan.angles)]
    )
    return apply(_reference_world_transform(scan), p_laser)


def _deskewed_points_world(scan) -> np.ndarray:
    result = deskew_scan(
        point_times=scan.point_times,
        angles=scan.angles,
        ranges=scan.ranges,
        pose_times=scan.pose_times,
        pose_xytheta=scan.pose_xytheta,
        reference_time=scan.reference_time,
        extrinsic_xytheta=scan.extrinsic,
    )
    return apply(_reference_world_transform(scan), result.points_xy)


def test_static_scan_is_identity():
    scan = simulate_wall_scan(vx=0.0, vy=0.0, omega=0.0)
    result = deskew_scan(
        scan.point_times, scan.angles, scan.ranges,
        scan.pose_times, scan.pose_xytheta,
        scan.reference_time, scan.extrinsic,
    )
    p_laser = np.column_stack(
        [scan.ranges * np.cos(scan.angles), scan.ranges * np.sin(scan.angles)]
    )
    assert np.allclose(result.points_xy, p_laser, atol=1e-10)


def test_wall_residual_drops_after_deskew():
    scan = simulate_wall_scan()  # constant translation + rotation
    naive = _naive_points_world(scan)
    fixed = _deskewed_points_world(scan)

    naive_res = np.abs(naive[:, 0] - scan.wall_x)
    fixed_res = np.abs(fixed[:, 0] - scan.wall_x)

    # Ground truth is exact (constant velocity + linear interpolation), so
    # the corrected residual should be at numerical-noise level.
    assert fixed_res.max() < 1e-8
    # And it must be a large improvement over the distorted scan.
    assert naive_res.max() > 1e-2
    assert fixed_res.mean() < naive_res.mean() * 1e-3


def test_wall_residual_drops_with_rotation_only():
    scan = simulate_wall_scan(vx=0.0, vy=0.0, omega=1.5)
    naive_res = np.abs(_naive_points_world(scan)[:, 0] - scan.wall_x)
    fixed_res = np.abs(_deskewed_points_world(scan)[:, 0] - scan.wall_x)
    assert fixed_res.max() < 1e-8
    assert naive_res.max() > 1e-3


def test_residual_improvement_robust_to_noise():
    scan = simulate_wall_scan(range_noise_std=0.01, seed=42)
    naive_res = np.abs(_naive_points_world(scan)[:, 0] - scan.wall_x)
    fixed_res = np.abs(_deskewed_points_world(scan)[:, 0] - scan.wall_x)
    assert fixed_res.mean() < naive_res.mean()
    # Residual should be on the order of the injected noise.
    assert fixed_res.mean() < 0.02


def test_missing_pose_coverage_rejected():
    scan = simulate_wall_scan()
    # Drop the pose coverage for the second half of the scan.
    half = len(scan.pose_times) // 2
    with pytest.raises(PoseCoverageError, match="outside pose coverage"):
        deskew_scan(
            scan.point_times, scan.angles, scan.ranges,
            scan.pose_times[:half], scan.pose_xytheta[:half],
            scan.reference_time, scan.extrinsic,
        )


def test_reference_time_outside_coverage_rejected():
    scan = simulate_wall_scan()
    with pytest.raises(PoseCoverageError):
        deskew_scan(
            scan.point_times, scan.angles, scan.ranges,
            scan.pose_times, scan.pose_xytheta,
            reference_time=scan.pose_times[-1] + 1.0,
            extrinsic_xytheta=scan.extrinsic,
        )


def test_mismatched_point_arrays_rejected():
    scan = simulate_wall_scan()
    with pytest.raises(ValueError):
        deskew_scan(
            scan.point_times[:-1], scan.angles, scan.ranges,
            scan.pose_times, scan.pose_xytheta,
            scan.reference_time, scan.extrinsic,
        )
