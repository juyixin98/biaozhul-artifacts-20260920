"""四元数工具测试。"""

import numpy as np
import pytest

from imu_integrator.quaternion import (
    quat_multiply,
    quat_normalize,
    quat_rotate,
    quat_conjugate,
    quat_to_rotation_matrix,
    quat_from_angle_axis,
)


def test_normalize():
    q = quat_normalize([0.0, 2.0, 0.0, 0.0])
    np.testing.assert_allclose(q, [0.0, 1.0, 0.0, 0.0], atol=1e-15)


def test_normalize_zero_raises():
    with pytest.raises(ValueError):
        quat_normalize([0.0, 0.0, 0.0, 0.0])


def test_rotate_identity():
    v = np.array([1.0, -2.0, 3.0])
    np.testing.assert_allclose(quat_rotate([1, 0, 0, 0], v), v, atol=1e-15)


@pytest.mark.parametrize("axis,angle", [
    ([1, 0, 0], 0.7),
    ([0, 1, 0], -1.3),
    ([0, 0, 1], 2.1),
    ([1, 1, 0], 0.9),
])
def test_rotate_matches_rotation_matrix(axis, angle):
    q = quat_from_angle_axis(angle, axis)
    v = np.array([0.5, -1.2, 2.3])
    r = quat_to_rotation_matrix(q)
    np.testing.assert_allclose(quat_rotate(q, v), r @ v, atol=1e-12)


def test_rotate_90_deg_about_z():
    q = quat_from_angle_axis(np.pi / 2, [0, 0, 1])
    np.testing.assert_allclose(quat_rotate(q, [1, 0, 0]), [0, 1, 0], atol=1e-12)
    np.testing.assert_allclose(quat_rotate(q, [0, 1, 0]), [-1, 0, 0], atol=1e-12)


def test_compose_rotations_about_z():
    q1 = quat_from_angle_axis(0.3, [0, 0, 1])
    q2 = quat_from_angle_axis(0.4, [0, 0, 1])
    q = quat_multiply(q1, q2)
    expected = quat_from_angle_axis(0.7, [0, 0, 1])
    np.testing.assert_allclose(q, expected, atol=1e-12)


def test_conjugate_inverts_rotation():
    q = quat_from_angle_axis(0.6, [0, 1, 0])
    v = np.array([1.0, 0.5, -0.25])
    np.testing.assert_allclose(quat_rotate(quat_conjugate(q), quat_rotate(q, v)),
                               v, atol=1e-12)


def test_rotation_matrix_orthonormal_determinant_one():
    q = quat_normalize([0.3, -1.0, 0.5, 0.2])
    r = quat_to_rotation_matrix(q)
    np.testing.assert_allclose(r.T @ r, np.eye(3), atol=1e-12)
    assert np.linalg.det(r) == pytest.approx(1.0, abs=1e-12)


def test_angle_axis_zero_axis_zero_angle_is_identity():
    np.testing.assert_allclose(
        quat_from_angle_axis(0.0, [0, 0, 0]), [1, 0, 0, 0], atol=1e-15
    )


def test_angle_axis_zero_axis_nonzero_angle_raises():
    with pytest.raises(ValueError):
        quat_from_angle_axis(1.0, [0, 0, 0])
