"""Transform：四元数、逆变换、复合、slerp 的单元测试。"""

import numpy as np
import pytest

from transform_tree.errors import InvalidTransformError
from transform_tree.transform import Transform, quaternion_slerp


def test_identity_transform_leaves_point_in_place():
    p = np.array([1.0, -2.0, 3.0])
    np.testing.assert_allclose(Transform.identity().transform_point(p), p)


def test_compose_and_inverse_round_trip():
    a = Transform.from_quaternion(
        np.array([np.cos(np.pi / 8), 0, 0, np.sin(np.pi / 8)]), [1.0, 2.0, 3.0]
    )
    b = Transform.from_quaternion(
        np.array([np.cos(np.pi / 6), 0, np.sin(np.pi / 6), 0]), [-1.0, 0.5, 2.0]
    )
    composed = a @ b
    round_trip = composed @ b.inverse()
    assert round_trip.is_close(a, atol=1e-10)


def test_inverse_matches_analytic_form():
    t = Transform.from_quaternion([np.cos(0.3), 0, 0, np.sin(0.3)], [0.4, -0.2, 0.9])
    inv = t.inverse()
    product = t @ inv
    assert product.is_close(Transform.identity())
    np.testing.assert_allclose(inv.rotation, t.rotation.T, atol=1e-12)
    np.testing.assert_allclose(inv.translation, -(t.rotation.T @ t.translation), atol=1e-12)


def test_inverse_transforms_point_back():
    t = Transform.from_quaternion([np.cos(0.5), 0.3, -0.2, 0.7], [1.0, -2.0, 0.5])
    p = np.array([0.7, -1.3, 2.1])
    np.testing.assert_allclose(t.inverse().transform_point(t.transform_point(p)), p, atol=1e-12)


def test_matrix_round_trip_preserves_transform():
    t = Transform.from_quaternion([0.5, 0.5, 0.5, 0.5], [3.0, 0.0, -1.0])
    again = Transform.from_matrix(t.to_matrix())
    assert again.is_close(t)


def test_quaternion_construction_normalizes():
    t = Transform.from_quaternion([2.0, 0.0, 0.0, 0.0])
    np.testing.assert_allclose(t.rotation, np.eye(3), atol=1e-12)


def test_reject_reflection_matrix():
    reflected = np.diag([-1.0, 1.0, 1.0])
    with pytest.raises(InvalidTransformError):
        Transform(reflected, np.zeros(3))


def test_reject_non_orthogonal_matrix():
    bad = np.array([[1.0, 0.1, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    with pytest.raises(InvalidTransformError):
        Transform(bad, np.zeros(3))


def test_reject_non_finite_values():
    with pytest.raises(InvalidTransformError):
        Transform(np.eye(3), [1.0, np.nan, 0.0])


def test_reject_zero_quaternion():
    with pytest.raises(InvalidTransformError):
        Transform.from_quaternion([0.0, 0.0, 0.0, 0.0])


def test_slerp_endpoints():
    q0 = np.array([1.0, 0.0, 0.0, 0.0])
    q1 = np.array([np.cos(np.pi / 4), 0.0, 0.0, np.sin(np.pi / 4)])
    np.testing.assert_allclose(quaternion_slerp(q0, q1, 0.0), q0, atol=1e-12)
    np.testing.assert_allclose(quaternion_slerp(q0, q1, 1.0), q1, atol=1e-12)


def test_slerp_midpoint_is_45_degrees():
    q0 = np.array([1.0, 0.0, 0.0, 0.0])
    q1 = np.array([np.cos(np.pi / 4), 0.0, 0.0, np.sin(np.pi / 4)])  # 姿态角 90°
    q_mid = quaternion_slerp(q0, q1, 0.5)
    t_mid = Transform.from_quaternion(q_mid)
    rotated = t_mid.transform_point([1.0, 0.0, 0.0])
    # 姿态角中点为 45°（不是 22.5°：四元数分量是半角）。
    np.testing.assert_allclose(rotated, [np.cos(np.pi / 4), np.sin(np.pi / 4), 0.0], atol=1e-12)


def test_slerp_takes_shortest_arc_for_antipodal_sign():
    q0 = np.array([1.0, 0.0, 0.0, 0.0])
    # -q1 与 q1 表示同一旋转（姿态角 60°）；最短弧插值不应绕远路。
    q1 = -np.array([np.cos(np.pi / 6), 0.0, 0.0, np.sin(np.pi / 6)])
    q_mid = quaternion_slerp(q0, q1, 0.5)
    assert q_mid[0] > 0.0
    # 姿态角中点为 30°。
    np.testing.assert_allclose(
        Transform.from_quaternion(q_mid).transform_point([1, 0, 0]),
        [np.cos(np.pi / 6), np.sin(np.pi / 6), 0.0],
        atol=1e-12,
    )


def test_slerp_small_angle_does_not_explode():
    q0 = np.array([1.0, 0.0, 0.0, 0.0])
    q1 = np.array([np.cos(1e-9), 0.0, 0.0, np.sin(1e-9)])
    q_mid = quaternion_slerp(q0, q1, 0.5)
    assert np.all(np.isfinite(q_mid))
    np.testing.assert_allclose(np.linalg.norm(q_mid), 1.0)
