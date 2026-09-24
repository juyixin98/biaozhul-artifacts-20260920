"""Verify the analytic edge Jacobians against central finite differences."""

import numpy as np

from pose_graph.optimizer import edge_error_and_jacobians


def _numeric_jacobians(xi, xj, z, eps=1e-7):
    A = np.zeros((3, 3))
    B = np.zeros((3, 3))
    # Central differences, component by component.
    for k in range(3):
        d = np.zeros(3)
        d[k] = eps
        e_plus, _, _ = edge_error_and_jacobians(xi + d, xj, z)
        e_minus, _, _ = edge_error_and_jacobians(xi - d, xj, z)
        A[:, k] = (e_plus - e_minus) / (2 * eps)
        e_plus, _, _ = edge_error_and_jacobians(xi, xj + d, z)
        e_minus, _, _ = edge_error_and_jacobians(xi, xj - d, z)
        B[:, k] = (e_plus - e_minus) / (2 * eps)
    return A, B


def test_jacobians_match_numeric():
    rng = np.random.default_rng(42)
    cases = 0
    while cases < 200:
        xi = rng.uniform(-5, 5, size=3)
        xj = rng.uniform(-5, 5, size=3)
        z = rng.uniform(-3, 3, size=3)
        # Skip configurations where the angular error sits on the wrap
        # discontinuity; finite differences are meaningless there.
        e, A, B = edge_error_and_jacobians(xi, xj, z)
        if abs(abs(e[2]) - np.pi) < 1e-3:
            continue
        A_num, B_num = _numeric_jacobians(xi, xj, z)
        np.testing.assert_allclose(A, A_num, rtol=1e-4, atol=1e-5)
        np.testing.assert_allclose(B, B_num, rtol=1e-4, atol=1e-5)
        cases += 1


def test_error_is_zero_at_consistent_poses():
    rng = np.random.default_rng(7)
    for _ in range(50):
        xi = rng.normal(size=3)
        z = rng.normal(size=3)
        # Build xj so that xi^-1 (+) xj == z exactly.
        from pose_graph.se2 import compose

        xj = compose(xi, z)
        e, _, _ = edge_error_and_jacobians(xi, xj, z)
        assert np.allclose(e, np.zeros(3), atol=1e-12)
