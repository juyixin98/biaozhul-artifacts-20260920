import numpy as np
import pytest

from app.interpolation import PoseCoverageError, interpolate_pose


def test_linear_midpoint():
    times = np.array([0.0, 1.0])
    poses = np.array([[0.0, 0.0, 0.0], [2.0, 4.0, 0.5]])
    out = interpolate_pose(times, poses, np.array([0.5]))
    assert np.allclose(out[0], [1.0, 2.0, 0.25], atol=1e-12)


def test_heading_unwrapped_across_pi():
    times = np.array([0.0, 1.0])
    poses = np.array([[0.0, 0.0, np.pi - 0.1], [0.0, 0.0, -np.pi + 0.1]])
    out = interpolate_pose(times, poses, np.array([0.5]))
    # Should interpolate through +/-pi, not the long way through 0.
    assert abs(abs(out[0, 2]) - np.pi) < 1e-9


def test_outside_coverage_rejected():
    times = np.array([1.0, 2.0])
    poses = np.zeros((2, 3))
    with pytest.raises(PoseCoverageError):
        interpolate_pose(times, poses, np.array([0.5]))
    with pytest.raises(PoseCoverageError):
        interpolate_pose(times, poses, np.array([2.5]))


def test_single_pose_sample_rejected():
    with pytest.raises(PoseCoverageError):
        interpolate_pose(np.array([1.0]), np.zeros((1, 3)), np.array([1.0]))


def test_non_increasing_times_rejected():
    with pytest.raises(ValueError):
        interpolate_pose(
            np.array([1.0, 1.0]), np.zeros((2, 3)), np.array([1.0])
        )
