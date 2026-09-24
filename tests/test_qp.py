"""Tests for the active-set QP solver, cross-checked against SLSQP as an
independent general-purpose constrained solver."""

import numpy as np
import pytest
from scipy.optimize import minimize

from app.qp import find_feasible, solve_qp


def _slsqp_qp(H, g, A, b):
    """Independent reference: solve the QP with scipy's SLSQP."""
    n = H.shape[0]
    res = minimize(
        lambda x: 0.5 * x @ H @ x + g @ x,
        x0=np.zeros(n),
        jac=lambda x: H @ x + g,
        constraints=[{"type": "ineq", "fun": lambda x: b - A @ x,
                      "jac": lambda x: -A}],
        method="SLSQP",
        options={"ftol": 1e-12, "maxiter": 500},
    )
    return res


def _random_qp(rng, n, m):
    """Random strictly-convex QP with a guaranteed non-empty feasible set."""
    M = rng.standard_normal((n, n))
    H = M.T @ M + n * np.eye(n)
    g = rng.standard_normal(n)
    A = rng.standard_normal((m, n))
    x_feas = rng.standard_normal(n)
    b = A @ x_feas + rng.uniform(0.1, 2.0, size=m)  # x_feas strictly feasible
    return H, g, A, b


@pytest.mark.parametrize("seed", range(8))
def test_matches_slsqp(seed):
    rng = np.random.default_rng(seed)
    H, g, A, b = _random_qp(rng, n=6, m=10)
    res = solve_qp(H, g, A, b)
    assert res.status == "optimal"
    ref = _slsqp_qp(H, g, A, b)
    assert ref.success
    obj_ref = 0.5 * ref.x @ H @ ref.x + g @ ref.x
    assert res.objective == pytest.approx(obj_ref, rel=1e-6, abs=1e-8)
    assert np.all(A @ res.x <= b + 1e-7)


def test_unconstrained():
    rng = np.random.default_rng(0)
    M = rng.standard_normal((4, 4))
    H = M.T @ M + np.eye(4)
    g = rng.standard_normal(4)
    res = solve_qp(H, g, np.zeros((0, 4)), np.zeros(0))
    assert res.status == "optimal"
    assert np.allclose(res.x, np.linalg.solve(H, -g))


def test_active_constraint_at_optimum():
    # min x^2 + y^2 s.t. x + y >= 2  ->  optimum (1, 1)
    H = 2.0 * np.eye(2)
    g = np.zeros(2)
    A = np.array([[-1.0, -1.0]])
    b = np.array([-2.0])
    res = solve_qp(H, g, A, b)
    assert res.status == "optimal"
    assert np.allclose(res.x, [1.0, 1.0], atol=1e-8)
    assert res.lam[0] == pytest.approx(2.0, abs=1e-8)


def test_infeasible_detected():
    # x <= 0 and x >= 1 simultaneously: empty polytope.
    H = np.eye(1)
    g = np.zeros(1)
    A = np.array([[1.0], [-1.0]])
    b = np.array([0.0, -1.0])
    res = solve_qp(H, g, A, b)
    assert res.status == "infeasible"
    assert res.x is None
    assert find_feasible(A, b) is None


def test_degenerate_redundant_constraints():
    # Duplicate constraints (degenerate active set) must still converge.
    H = 2.0 * np.eye(2)
    g = np.zeros(2)
    A = np.array([[-1.0, -1.0], [-1.0, -1.0], [-2.0, -2.0]])
    b = np.array([-2.0, -2.0, -4.0])
    res = solve_qp(H, g, A, b)
    assert res.status == "optimal"
    assert np.allclose(res.x, [1.0, 1.0], atol=1e-7)
