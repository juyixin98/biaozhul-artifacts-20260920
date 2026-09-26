"""四元数工具函数的单元测试。"""

import numpy as np
import pytest

from imu_preintegration import quaternion as quat


def test_normalize_returns_unit_quaternion():
    q = quat.normalize(np.array([2.0, 0.0, 0.0, 0.0]))
    assert np.isclose(np.linalg.norm(q), 1.0)
    assert np.allclose(q, [1.0, 0.0, 0.0, 0.0])


def test_normalize_rejects_zero_quaternion():
    with pytest.raises(ValueError):
        quat.normalize(np.zeros(4))


def test_from_rotvec_zero_rotation_is_identity():
    q = quat.from_rotvec(np.zeros(3))
    assert np.allclose(q, [1.0, 0.0, 0.0, 0.0], atol=1e-12)


def test_from_rotvec_90deg_about_z():
    q = quat.from_rotvec(np.array([0.0, 0.0, np.pi / 2]))
    rotated = quat.rotate(q, np.array([1.0, 0.0, 0.0]))
    assert np.allclose(rotated, [0.0, 1.0, 0.0], atol=1e-12)


def test_multiply_composes_rotations():
    qx = quat.from_rotvec(np.array([np.pi / 2, 0.0, 0.0]))
    qz = quat.from_rotvec(np.array([0.0, 0.0, np.pi / 2]))
    q = quat.multiply(qz, qx)  # 先绕 x，再绕 z
    rotated = quat.rotate(q, np.array([1.0, 0.0, 0.0]))
    expected = quat.rotate(qz, quat.rotate(qx, np.array([1.0, 0.0, 0.0])))
    assert np.allclose(rotated, expected, atol=1e-12)


def test_conjugate_inverts_rotation():
    q = quat.from_rotvec(np.array([0.3, -0.5, 0.8]))
    v = np.array([1.0, 2.0, 3.0])
    assert np.allclose(quat.rotate(quat.conjugate(q), quat.rotate(q, v)), v, atol=1e-12)


def test_to_matrix_is_orthonormal():
    q = quat.from_rotvec(np.array([0.1, 0.2, -0.3]))
    r = quat.to_matrix(q)
    assert np.allclose(r @ r.T, np.eye(3), atol=1e-12)
    assert np.isclose(np.linalg.det(r), 1.0)
