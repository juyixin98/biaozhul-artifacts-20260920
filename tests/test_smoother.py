"""Tests for the trajectory smoother, cross-checked against an independent
general-purpose QP/NLP solver (scipy SLSQP) on the full stacked problem."""

import numpy as np
import pytest
from scipy.optimize import minimize

from app.smoother import Corridor, smooth_trajectory


def box_corridor(d, lo, hi):
    """Axis-aligned box corridor lo <= x <= hi as A x <= b."""
    A = np.vstack([np.eye(d), -np.eye(d)])
    b = np.concatenate([np.asarray(hi, float), -np.asarray(lo, float)])
    return Corridor(A, b)


def free_corridor(d):
    return Corridor(np.zeros((0, d)), np.zeros(0))


def slsqp_reference(z, corridors, lam, w, start, end):
    """Independent reference solution on the full stacked problem via SLSQP."""
    n, d = z.shape
    N = n * d

    def unpack(x):
        return x.reshape(n, d)

    def obj(x):
        path = unpack(x)
        o = np.sum(w[:, None] * (path - z) ** 2)
        if n >= 3:
            d2 = path[:-2] - 2 * path[1:-1] + path[2:]
            o += lam * np.sum(d2**2)
        return o

    cons = []
    for i, corr in enumerate(corridors):
        if corr.A.shape[0]:
            cons.append({"type": "ineq",
                         "fun": lambda x, i=i, c=corr: c.b - c.A @ x[i * d:(i + 1) * d]})
    for idx, pt in ((0, start), (n - 1, end)):
        cons.append({"type": "eq", "fun": lambda x, idx=idx, pt=pt: x[idx * d:(idx + 1) * d] - pt})

    x0 = np.linspace(start, end, n).reshape(-1)
    res = minimize(obj, x0, constraints=cons, method="SLSQP",
                   options={"ftol": 1e-12, "maxiter": 1000})
    return res


def make_problem(seed=0, n=6, d=2, lam=1.0):
    rng = np.random.default_rng(seed)
    t = np.linspace(0, 1, n)
    base = np.column_stack([3 * t, np.sin(2 * np.pi * t)])
    z = base + 0.1 * rng.standard_normal((n, d))
    start, end = base[0], base[-1]
    corridors = [box_corridor(d, lo=base[i] - 0.5, hi=base[i] + 0.5) for i in range(n)]
    w = np.ones(n)
    return z, corridors, lam, w, start, end


# ---------------------------------------------------------------------------

def test_matches_independent_solver():
    z, corridors, lam, w, start, end = make_problem()
    res = smooth_trajectory(z, corridors, lam, w, start, end)
    assert res.status == "optimal"
    ref = slsqp_reference(z, corridors, lam, w, start, end)
    assert ref.success
    assert res.objective == pytest.approx(ref.fun, rel=1e-5, abs=1e-8)


def test_kkt_residuals_are_small():
    z, corridors, lam, w, start, end = make_problem(seed=3)
    res = smooth_trajectory(z, corridors, lam, w, start, end)
    assert res.status == "optimal"
    for key, val in res.residuals.items():
        assert val < 1e-6, f"{key} = {val}"


def test_unconstrained_smooths_noise():
    # Noisy samples of a straight line; with no corridors the smoothed path
    # must have a lower second-difference energy than the observations.
    n, d = 10, 2
    rng = np.random.default_rng(1)
    t = np.linspace(0, 1, n)
    line = np.column_stack([t, 2 * t])
    z = line + 0.05 * rng.standard_normal((n, d))
    corridors = [free_corridor(d) for _ in range(n)]
    res = smooth_trajectory(z, corridors, 10.0, np.ones(n), line[0], line[-1])
    assert res.status == "optimal"

    def energy(p):
        return np.sum((p[:-2] - 2 * p[1:-1] + p[2:]) ** 2)

    assert energy(res.path) < energy(z)
    assert np.allclose(res.path[0], line[0])
    assert np.allclose(res.path[-1], line[-1])


def test_corridor_forces_detour():
    # A wall between start and end forces the path off the straight line.
    n, d = 5, 2
    z = np.column_stack([np.linspace(0, 4, n), np.zeros(n)])
    corridors = [free_corridor(d) for _ in range(n)]
    # Middle points must stay above y = 1 (i.e. -y <= -1).
    for i in (1, 2, 3):
        corridors[i] = Corridor(np.array([[0.0, -1.0]]), np.array([-1.0]))
    res = smooth_trajectory(z, corridors, 1.0, np.ones(n), z[0], z[-1])
    assert res.status == "optimal"
    assert np.all(res.path[1:4, 1] >= 1.0 - 1e-8)
    assert np.max(np.abs(res.path[:, 1])) > 0.5  # actually detoured


def test_conflicting_corridors_are_infeasible():
    # Corridor for point 2 demands x <= 0 and x >= 1: empty polytope.
    n, d = 4, 2
    z = np.zeros((n, d))
    corridors = [free_corridor(d) for _ in range(n)]
    corridors[2] = Corridor(
        A=np.array([[1.0, 0.0], [-1.0, 0.0]]),
        b=np.array([0.0, -1.0]),  # x <= 0 and x >= 1
    )
    res = smooth_trajectory(z, corridors, 1.0, np.ones(n), z[0], z[-1])
    assert res.status == "infeasible"
    assert not res.feasible
    assert res.path is None  # never emit a path for an infeasible problem
    assert res.objective is None


def test_endpoint_outside_corridor_is_infeasible():
    n, d = 4, 2
    z = np.zeros((n, d))
    corridors = [free_corridor(d) for _ in range(n)]
    corridors[0] = box_corridor(d, lo=[-1.0, -1.0], hi=[1.0, 1.0])
    start = np.array([5.0, 0.0])  # violates corridor 0
    res = smooth_trajectory(z, corridors, 1.0, np.ones(n), start, z[-1])
    assert res.status == "infeasible"
    assert res.path is None


def test_duplicate_points():
    # Repeated observations (duplicate points) must not break the solver.
    n, d = 6, 2
    z = np.array([[0, 0], [1, 1], [1, 1], [1, 1], [2, 0], [3, 0]], dtype=float)
    corridors = [free_corridor(d) for _ in range(n)]
    res = smooth_trajectory(z, corridors, 2.0, np.ones(n), z[0], z[-1])
    assert res.status == "optimal"
    assert np.all(np.isfinite(res.path))
    ref = slsqp_reference(z, corridors, 2.0, np.ones(n), z[0], z[-1])
    assert res.objective == pytest.approx(ref.fun, rel=1e-5, abs=1e-8)


def test_duplicate_points_with_corridors():
    n, d = 6, 2
    z = np.array([[0, 0], [1, 1], [1, 1], [1, 1], [2, 0], [3, 0]], dtype=float)
    corridors = [box_corridor(d, lo=z[i] - 0.3, hi=z[i] + 0.3) for i in range(n)]
    res = smooth_trajectory(z, corridors, 2.0, np.ones(n), z[0], z[-1])
    assert res.status == "optimal"
    for i, corr in enumerate(corridors):
        assert np.all(corr.A @ res.path[i] <= corr.b + 1e-7)


def test_wide_weight_span():
    # Observation weights spanning 1e-6 .. 1e6: solution must stay optimal.
    n, d = 6, 2
    rng = np.random.default_rng(7)
    z = rng.standard_normal((n, d))
    w = np.logspace(-6, 6, n)
    corridors = [free_corridor(d) for _ in range(n)]
    res = smooth_trajectory(z, corridors, 1.0, w, z[0], z[-1])
    assert res.status == "optimal"
    assert res.residuals["stationarity"] < 1e-4
    # High-weight points are tracked almost exactly.
    assert np.linalg.norm(res.path[-2] - z[-2]) < 1e-2


def test_wide_weight_span_with_corridors():
    n, d = 6, 2
    rng = np.random.default_rng(11)
    z = rng.standard_normal((n, d))
    w = np.logspace(-4, 4, n)
    corridors = [box_corridor(d, lo=z[i] - 0.4, hi=z[i] + 0.4) for i in range(n)]
    res = smooth_trajectory(z, corridors, 1.0, w, z[0], z[-1])
    assert res.status == "optimal"
    assert res.residuals["primal_infeasibility"] < 1e-7
    ref = slsqp_reference(z, corridors, 1.0, w, z[0], z[-1])
    assert res.objective == pytest.approx(ref.fun, rel=1e-4, abs=1e-7)


def test_two_points_no_interior():
    z = np.array([[0.0, 0.0], [1.0, 1.0]])
    corridors = [free_corridor(2), free_corridor(2)]
    res = smooth_trajectory(z, corridors, 1.0, np.ones(2), z[0], z[-1])
    assert res.status == "optimal"
    assert np.allclose(res.path, z)


def test_smooth_weight_zero():
    # lambda = 0 collapses to weighted projection onto the corridors.
    n, d = 4, 2
    z = np.array([[0.0, 0.0], [5.0, 5.0], [5.0, 5.0], [1.0, 0.0]])
    corridors = [free_corridor(d) for _ in range(n)]
    corridors[1] = box_corridor(d, lo=[0.0, 0.0], hi=[1.0, 1.0])
    res = smooth_trajectory(z, corridors, 0.0, np.ones(n), z[0], z[-1])
    assert res.status == "optimal"
    assert np.allclose(res.path[1], [1.0, 1.0], atol=1e-8)  # clipped to box
