"""Core deskew tests on synthetic straight-wall scans."""

import numpy as np
import pytest

from app.deskew import (
    PoseCoverageError,
    PoseSequenceError,
    deskew_scan,
    interpolate_poses,
)
from app.synthetic import (
    naive_scan_points,
    simulate_wall_scan,
    wall_rmse_in_world,
)


def _deskew(scan):
    return deskew_scan(
        ranges=scan.ranges,
        angles=scan.angles,
        point_times=scan.point_times,
        pose_times=scan.pose_times,
        poses=scan.poses,
        reference_time=scan.reference_time,
        extrinsic=scan.extrinsic,
    )


def test_residual_drops_with_translation_and_rotation():
    """Uniform translation + rotation: deskewed points fit the wall far
    better than the raw (distorted) cloud."""
    scan = simulate_wall_scan()  # v=(1.0, 0.3) m/s, omega=0.5 rad/s
    rmse_naive = wall_rmse_in_world(naive_scan_points(scan), scan)
    rmse_deskewed = wall_rmse_in_world(_deskew(scan), scan)

    assert rmse_naive > 1e-2  # motion actually distorted the scan
    assert rmse_deskewed < 1e-9  # exact for constant-velocity motion
    assert rmse_deskewed < rmse_naive * 1e-6


def test_residual_drops_translation_only():
    scan = simulate_wall_scan(velocity=(1.5, -0.2), omega=0.0)
    rmse_naive = wall_rmse_in_world(naive_scan_points(scan), scan)
    rmse_deskewed = wall_rmse_in_world(_deskew(scan), scan)
    assert rmse_naive > 1e-2
    assert rmse_deskewed < 1e-9


def test_residual_drops_rotation_only():
    scan = simulate_wall_scan(velocity=(0.0, 0.0), omega=1.0)
    rmse_naive = wall_rmse_in_world(naive_scan_points(scan), scan)
    rmse_deskewed = wall_rmse_in_world(_deskew(scan), scan)
    assert rmse_naive > 1e-3
    assert rmse_deskewed < 1e-9


def test_reference_time_at_scan_end():
    scan = simulate_wall_scan(reference_time=0.1)
    rmse_deskewed = wall_rmse_in_world(_deskew(scan), scan)
    assert rmse_deskewed < 1e-9


def test_static_scan_is_identity():
    scan = simulate_wall_scan(velocity=(0.0, 0.0), omega=0.0)
    corrected = _deskew(scan)
    expected = naive_scan_points(scan)
    np.testing.assert_allclose(corrected, expected, atol=1e-12)


def test_missing_pose_coverage_at_scan_end_rejected():
    scan = simulate_wall_scan()
    # Drop the tail poses so the last point times are uncovered.
    keep = scan.pose_times <= scan.point_times.max() - 0.02
    with pytest.raises(PoseCoverageError, match="refusing to extrapolate"):
        deskew_scan(
            ranges=scan.ranges,
            angles=scan.angles,
            point_times=scan.point_times,
            pose_times=scan.pose_times[keep],
            poses=scan.poses[keep],
            reference_time=scan.reference_time,
            extrinsic=scan.extrinsic,
        )


def test_missing_pose_coverage_at_scan_start_rejected():
    scan = simulate_wall_scan()
    keep = scan.pose_times >= scan.point_times.min() + 0.02
    with pytest.raises(PoseCoverageError):
        deskew_scan(
            ranges=scan.ranges,
            angles=scan.angles,
            point_times=scan.point_times,
            pose_times=scan.pose_times[keep],
            poses=scan.poses[keep],
            reference_time=scan.reference_time,
            extrinsic=scan.extrinsic,
        )


def test_reference_time_outside_coverage_rejected():
    scan = simulate_wall_scan()
    with pytest.raises(PoseCoverageError):
        deskew_scan(
            ranges=scan.ranges,
            angles=scan.angles,
            point_times=scan.point_times,
            pose_times=scan.pose_times,
            poses=scan.poses,
            reference_time=scan.pose_times[-1] + 0.5,
            extrinsic=scan.extrinsic,
        )


def test_malformed_pose_sequence_rejected():
    scan = simulate_wall_scan()
    with pytest.raises(PoseSequenceError):
        deskew_scan(
            ranges=scan.ranges,
            angles=scan.angles,
            point_times=scan.point_times,
            pose_times=scan.pose_times[::-1],  # not increasing
            poses=scan.poses[::-1],
            reference_time=scan.reference_time,
            extrinsic=scan.extrinsic,
        )
    with pytest.raises(PoseSequenceError):
        deskew_scan(
            ranges=scan.ranges,
            angles=scan.angles,
            point_times=scan.point_times,
            pose_times=np.array([0.0]),  # too few poses
            poses=np.array([[0.0, 0.0, 0.0]]),
            reference_time=scan.reference_time,
            extrinsic=scan.extrinsic,
        )


def test_yaw_interpolation_wraps_across_pi():
    """Interpolating theta from +pi-eps to -pi+eps must pass through +/-pi,
    not swing the long way through 0."""
    pose_times = np.array([0.0, 1.0])
    eps = 0.01
    poses = np.array([[0.0, 0.0, np.pi - eps], [0.0, 0.0, -np.pi + eps]])
    mid = interpolate_poses(pose_times, poses, np.array([0.5]))[0]
    assert abs(abs(mid[2]) - np.pi) < 1e-6  # near +/-pi, i.e. cos ~ -1
    assert np.cos(mid[2]) == pytest.approx(-1.0, abs=1e-6)


def test_extrinsic_changes_result():
    """A non-trivial extrinsic must actually be applied."""
    scan_id = simulate_wall_scan(extrinsic=(0.0, 0.0, 0.0))
    scan_ext = simulate_wall_scan(extrinsic=(0.2, 0.1, 0.3))
    out_id = _deskew(scan_id)
    out_ext = _deskew(scan_ext)
    assert not np.allclose(out_id, out_ext)
    # Both still recover the wall exactly.
    assert wall_rmse_in_world(out_ext, scan_ext) < 1e-9
