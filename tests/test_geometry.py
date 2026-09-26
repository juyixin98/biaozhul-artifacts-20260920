"""Unit tests for SE(2) geometry helpers."""

import numpy as np

from icp2d.geometry import (
    compose_poses,
    inverse_pose,
    normalize_angle,
    pose_error,
    rotation_matrix,
    transform_points,
)


def test_rotation_matrix_orthogonal_and_det_one():
    r = rotation_matrix(0.7)
    np.testing.assert_allclose(r.T @ r, np.eye(2), atol=1e-12)
    assert np.isclose(np.linalg.det(r), 1.0)


def test_transform_points_roundtrip():
    points = np.array([[1.0, 0.0], [0.0, 1.0], [-2.0, 3.0]])
    pose = np.array([0.4, -0.9, 0.37])
    moved = transform_points(points, pose)
    back = transform_points(moved, inverse_pose(pose))
    np.testing.assert_allclose(back, points, atol=1e-12)


def test_compose_with_inverse_is_identity():
    pose = np.array([1.2, -0.6, 0.9])
    identity = compose_poses(inverse_pose(pose), pose)
    np.testing.assert_allclose(identity, np.zeros(3), atol=1e-12)


def test_compose_poses_translation_rule():
    a = np.array([1.0, 0.0, 0.0])
    b = np.array([0.0, 1.0, 0.0])
    result = compose_poses(a, b)
    np.testing.assert_allclose(result[:2], [1.0, 1.0])
    assert np.isclose(result[2], 0.0)


def test_normalize_angle_wraps_into_interval():
    assert np.isclose(normalize_angle(3.0 * np.pi), -np.pi)
    assert np.isclose(normalize_angle(-3.0 * np.pi), -np.pi)
    assert np.isclose(normalize_angle(2.5 * np.pi), np.pi / 2)
    wrapped = normalize_angle(7.3)
    assert -np.pi <= wrapped < np.pi


def test_pose_error_wraps_angle():
    error = pose_error(np.array([0.0, 0.0, 3.0 * np.pi]), np.zeros(3))
    np.testing.assert_allclose(error, [0.0, 0.0, np.pi], atol=1e-12)
