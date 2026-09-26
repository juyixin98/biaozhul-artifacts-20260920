"""Transform 与四元数运算的手算验证。"""

import numpy as np
import pytest

from transform_tree import Transform
from transform_tree.quaternion import from_axis_angle, rotate, slerp

SQRT2_2 = np.sqrt(2.0) / 2.0  # sin/cos(45°)


def test_compose_hand_computed():
    # a_T_b: 平移 (1,0,0)，无旋转；b_T_c: 绕 z 轴 +90°，平移 (0,1,0)
    a_t_b = Transform([1, 0, 0], [0, 0, 0, 1])
    b_t_c = Transform([0, 1, 0], [0, 0, SQRT2_2, SQRT2_2])
    a_t_c = a_t_b.compose(b_t_c)
    # 平移: R_ab @ (0,1,0) + (1,0,0) = I @ (0,1,0) + (1,0,0) = (1,1,0)
    np.testing.assert_allclose(a_t_c.translation, [1, 1, 0], atol=1e-12)
    np.testing.assert_allclose(a_t_c.rotation, [0, 0, SQRT2_2, SQRT2_2], atol=1e-12)
    # 点验证：c 系下 (1,0,0) -> b 系 (0,2,0) -> a 系 (1,2,0)
    np.testing.assert_allclose(a_t_c.apply([1, 0, 0]), [1, 2, 0], atol=1e-12)


def test_inverse_roundtrip():
    t = Transform([1, 2, 3], from_axis_angle([0, 0, 1], 0.7))
    identity = t.compose(t.inverse())
    np.testing.assert_allclose(identity.translation, [0, 0, 0], atol=1e-12)
    np.testing.assert_allclose(identity.rotation, [0, 0, 0, 1], atol=1e-12)


def test_apply_point():
    # 绕 z 轴 +90° 后点 (1,0,0) 变为 (0,1,0)，再加平移 (1,0,0)
    t = Transform([1, 0, 0], [0, 0, SQRT2_2, SQRT2_2])
    np.testing.assert_allclose(t.apply([1, 0, 0]), [1, 1, 0], atol=1e-12)


def test_slerp_halfway_is_half_angle():
    q0 = np.array([0.0, 0.0, 0.0, 1.0])
    q1 = from_axis_angle([0, 0, 1], np.pi / 2)
    mid = slerp(q0, q1, 0.5)
    np.testing.assert_allclose(
        rotate(mid, [1, 0, 0]), [SQRT2_2, SQRT2_2, 0], atol=1e-12
    )


def test_slerp_handles_opposite_sign():
    q0 = np.array([0.0, 0.0, 0.0, 1.0])
    q1 = -from_axis_angle([0, 0, 1], 0.2)  # 同一旋转的相反符号表示
    mid = slerp(q0, q1, 0.5)
    np.testing.assert_allclose(
        rotate(mid, [1, 0, 0]), [np.cos(0.1), np.sin(0.1), 0], atol=1e-12
    )


def test_invalid_quaternion_rejected():
    with pytest.raises(ValueError):
        Transform([0, 0, 0], [0, 0, 0, 0])
