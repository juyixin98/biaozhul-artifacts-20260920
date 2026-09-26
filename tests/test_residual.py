"""Analytic Jacobian verification against central finite differences."""

import numpy as np

from pose_graph.residual import edge_residual, edge_residual_and_jacobians


def _finite_difference_jacobians(pose_i, pose_j, z, h=1e-6):
    a = np.zeros((3, 3))
    b = np.zeros((3, 3))
    for col in range(3):
        p_i = np.array(pose_i, dtype=float)
        p_j = np.array(pose_j, dtype=float)
        p_i[col] += h
        r_plus = edge_residual(p_i, pose_j, z)
        p_i[col] -= 2 * h
        r_minus = edge_residual(p_i, pose_j, z)
        a[:, col] = (r_plus - r_minus) / (2 * h)

        p_i2 = np.array(pose_i, dtype=float)
        p_jp = np.array(pose_j, dtype=float)
        p_jp[col] += h
        r_plus = edge_residual(p_i2, p_jp, z)
        p_jp[col] -= 2 * h
        r_minus = edge_residual(p_i2, p_jp, z)
        b[:, col] = (r_plus - r_minus) / (2 * h)
    return a, b


def test_jacobians_match_finite_differences():
    cases = [
        ([0.0, 0.0, 0.1], [1.0, 0.2, 0.3], [1.0, 0.1, 0.2]),
        ([2.0, -1.0, 1.4], [3.0, 0.5, -2.0], [0.8, 1.2, -0.7]),
        ([-3.0, 4.0, -2.6], [-2.0, 3.0, 0.4], [1.0, -0.5, 2.5]),
    ]
    for pose_i, pose_j, z in cases:
        r, a, b = edge_residual_and_jacobians(pose_i, pose_j, z)
        a_fd, b_fd = _finite_difference_jacobians(pose_i, pose_j, z)
        np.testing.assert_allclose(a, a_fd, atol=1e-5, err_msg="A Jacobian mismatch")
        np.testing.assert_allclose(b, b_fd, atol=1e-5, err_msg="B Jacobian mismatch")
        assert r.shape == (3,)


def test_zero_residual_when_poses_match_measurement():
    from pose_graph.se2 import pose_compose

    pose_i = np.array([1.0, -2.0, 0.8])
    z = np.array([0.7, 0.3, 0.5])
    pose_j = pose_compose(pose_i, z)
    r = edge_residual(pose_i, pose_j, z)
    np.testing.assert_allclose(r, np.zeros(3), atol=1e-12)
