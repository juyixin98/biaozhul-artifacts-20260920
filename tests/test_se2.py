"""Tests for SE2 primitives and angle wrapping."""

import numpy as np

from pose_graph.se2 import (
    pose_compose,
    pose_inverse,
    relative_pose,
    rotation_matrix,
    wrap_angle,
)


def test_wrap_angle_scalar_and_array():
    assert wrap_angle(0.0) == 0.0
    # Convention: [-pi, pi); +pi maps to -pi (the same physical angle).
    assert abs(wrap_angle(3.0 * np.pi) - (-np.pi)) < 1e-12
    assert abs(wrap_angle(np.pi) - (-np.pi)) < 1e-12
    assert abs(wrap_angle(-np.pi) - (-np.pi)) < 1e-12
    np.testing.assert_allclose(
        wrap_angle(np.array([0.0, 2 * np.pi + 0.1, -2 * np.pi - 0.1])),
        np.array([0.0, 0.1, -0.1]),
        atol=1e-12,
    )


def test_rotation_matrix_orthogonal():
    for theta in [0.0, 0.7, -1.3, 3.1]:
        r = rotation_matrix(theta)
        np.testing.assert_allclose(r.T @ r, np.eye(2), atol=1e-12)
        np.testing.assert_allclose(r.T, rotation_matrix(-theta), atol=1e-12)


def test_compose_inverse_roundtrip():
    p = np.array([1.5, -0.8, 0.9])
    identity = pose_compose(p, pose_inverse(p))
    np.testing.assert_allclose(identity[:2], [0.0, 0.0], atol=1e-12)
    assert abs(identity[2]) < 1e-12


def test_relative_pose_consistency():
    p_i = np.array([1.0, 2.0, 0.5])
    p_j = np.array([3.0, -1.0, 2.0])
    z = relative_pose(p_i, p_j)
    # Reconstruct j from i and z.
    p_j_reconstructed = pose_compose(
        p_i, pose_compose(np.array([0.0, 0.0, 0.0]), z)
    )
    np.testing.assert_allclose(
        p_j_reconstructed[:2], p_j[:2], atol=1e-12
    )
    assert abs(wrap_angle(p_j_reconstructed[2] - p_j[2])) < 1e-12
    assert -np.pi < z[2] <= np.pi


def test_relative_pose_across_pi_cut():
    # theta_i ~ +pi/2, theta_j ~ -pi/2  => true relative angle is ~pi/-pi.
    z = relative_pose([0, 0, 1.5707], [1, 0, -1.5707])
    assert abs(abs(z[2]) - np.pi) < 1e-3
    assert -np.pi < z[2] <= np.pi
