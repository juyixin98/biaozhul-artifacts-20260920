"""Jacobian validation: analytic vs central finite differences."""

import numpy as np
import pytest

from pgo import Edge, edge_residual_and_jacobians, numerical_jacobian


@pytest.mark.parametrize("seed", range(25))
def test_edge_jacobians_match_numerical(seed):
    rng = np.random.default_rng(1000 + seed)
    poses = rng.uniform(-3.0, 3.0, size=(4, 3))
    poses[:, 2] = rng.uniform(-np.pi, np.pi, size=4)
    edge = Edge(
        i=int(rng.integers(0, 2)),
        j=int(rng.integers(2, 4)),
        measurement=np.array(
            [rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1)]
        ),
        information=np.eye(3),
    )
    _, ai, aj = edge_residual_and_jacobians(poses, edge)

    def residual_of(stack):
        p = poses.copy()
        p[edge.i] = stack[:3]
        p[edge.j] = stack[3:]
        r, _, _ = edge_residual_and_jacobians(p, edge)
        return r

    stack = np.concatenate([poses[edge.i], poses[edge.j]])
    num = numerical_jacobian(residual_of, stack, eps=1e-7)
    assert np.max(np.abs(ai - num[:, :3])) < 1e-5
    assert np.max(np.abs(aj - num[:, 3:])) < 1e-5


def test_jacobian_near_angle_wrap():
    # poses straddling the +/-pi branch cut: residual must stay smooth and
    # the angular jacobian entries remain exactly -1 / +1
    poses = np.array(
        [
            [0.0, 0.0, np.pi - 1e-3],
            [1.0, 0.0, -np.pi + 1e-3],
        ]
    )
    # true relative angle theta1 - theta0 = -2*pi + 2e-3, wrapped to +2e-3
    edge = Edge(0, 1, np.array([1.0, 0.0, 2e-3]), np.eye(3))
    r, ai, aj = edge_residual_and_jacobians(poses, edge)
    assert abs(r[2]) < 1e-6  # wrapped residual is tiny, not ~2*pi
    assert ai[2, 2] == -1.0 and aj[2, 2] == 1.0
