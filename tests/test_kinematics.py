"""正运动学与解析雅可比测试。"""

import math

import numpy as np
import pytest

from app.kinematics import (
    LinkParams,
    forward_kinematics,
    jacobian,
    wrap_to_pi,
)


@pytest.mark.parametrize(
    "q1,q2,expected",
    [
        (0.0, 0.0, (2.0, 0.0)),
        (math.pi / 2, 0.0, (0.0, 2.0)),
        (0.0, math.pi / 2, (1.0, 1.0)),
        (math.pi / 2, -math.pi / 2, (1.0, 1.0)),
        (0.0, math.pi, (0.0, 0.0)),
    ],
)
def test_forward_kinematics_known_values(arm, q1, q2, expected):
    ee = forward_kinematics(q1, q2, arm)
    assert ee == pytest.approx(expected, abs=1e-12)


def test_fk_uneven_links(uneven_arm):
    ee = forward_kinematics(0.0, 0.0, uneven_arm)
    assert ee == pytest.approx((2.0, 0.0), abs=1e-12)
    ee = forward_kinematics(0.0, math.pi, uneven_arm)
    assert ee == pytest.approx((0.4, 0.0), abs=1e-12)


def test_jacobian_matches_finite_difference(arm):
    rng = np.random.default_rng(7)
    eps = 1e-7
    for _ in range(50):
        q1 = rng.uniform(-math.pi, math.pi)
        q2 = rng.uniform(-math.pi, math.pi)
        J = jacobian(q1, q2, arm)
        f0 = forward_kinematics(q1, q2, arm)
        # 两列分别对 q1、q2 做有限差分
        J_fd = np.column_stack(
            [
                (forward_kinematics(q1 + eps, q2, arm) - f0) / eps,
                (forward_kinematics(q1, q2 + eps, arm) - f0) / eps,
            ]
        )
        assert J == pytest.approx(J_fd, abs=1e-6)


def test_jacobian_determinant_singularity(arm):
    # det(J) = l1 l2 sin(q2)
    assert abs(np.linalg.det(jacobian(0.7, 0.0, arm))) < 1e-12
    assert abs(np.linalg.det(jacobian(0.7, math.pi, arm))) < 1e-12
    assert abs(np.linalg.det(jacobian(0.0, math.pi / 2, arm))) - 1.0 < 1e-12


def test_wrap_to_pi():
    assert wrap_to_pi(0.0) == 0.0
    assert wrap_to_pi(3 * math.pi / 2) == pytest.approx(-math.pi / 2)
    assert wrap_to_pi(-3 * math.pi / 2) == pytest.approx(math.pi / 2, abs=1e-12)
    assert -math.pi <= wrap_to_pi(12345.6) < math.pi


def test_invalid_params_rejected():
    with pytest.raises(ValueError):
        LinkParams(l1=0.0, l2=1.0)
    with pytest.raises(ValueError):
        LinkParams(l1=1.0, l2=-1.0)
    with pytest.raises(ValueError):
        LinkParams(theta1_min=1.0, theta1_max=0.0)
