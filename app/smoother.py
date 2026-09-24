"""Offline sparse-trajectory smoothing as a strictly-convex QP.

Problem
-------
Given noisy observations z_0..z_{n-1} in R^d, find x_0..x_{n-1} minimising

    J(x) = sum_i w_i * ||x_i - z_i||^2
           + lambda * sum_{i=1..n-2} ||x_{i-1} - 2 x_i + x_{i+1}||^2

subject to
    x_i in C_i = { y : A_i y <= b_i }   (per-point convex corridor, optional)
    x_0 = start,  x_{n-1} = end          (fixed endpoints)

The endpoints are eliminated by substitution, leaving an unconstrained-size
QP in the interior points with block-diagonal corridor constraints, solved
by the active-set method in app.qp.  Because the corridor constraints act
per point and never couple points, feasibility is separable: the problem is
feasible iff every corridor is non-empty and both endpoints lie inside
their own corridors.  This module checks exactly that and never returns a
path for an infeasible problem.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .qp import find_feasible, solve_qp

__all__ = ["Corridor", "SmoothResult", "smooth_trajectory"]


@dataclass
class Corridor:
    """Polytope { y in R^d : A y <= b } for one trajectory point."""

    A: np.ndarray  # (m_i, d); may have 0 rows (no constraint)
    b: np.ndarray  # (m_i,)


@dataclass
class SmoothResult:
    status: str  # "optimal" | "infeasible" | "iteration_limit" | "numerical_error"
    feasible: bool
    path: np.ndarray | None  # (n, d); None unless status == "optimal"
    objective: float | None
    residuals: dict | None  # KKT residuals, None unless status == "optimal"
    iterations: int
    message: str = ""


def _objective(path: np.ndarray, z: np.ndarray, w: np.ndarray, lam: float) -> float:
    obs = float(np.sum(w[:, None] * (path - z) ** 2))
    if path.shape[0] >= 3:
        d2 = path[:-2] - 2.0 * path[1:-1] + path[2:]
        smooth = float(lam * np.sum(d2**2))
    else:
        smooth = 0.0
    return obs + smooth


def smooth_trajectory(
    observations: np.ndarray,
    corridors: list[Corridor],
    smooth_weight: float,
    obs_weights: np.ndarray,
    start: np.ndarray,
    end: np.ndarray,
    tol: float = 1e-9,
) -> SmoothResult:
    """Solve the smoothing QP.  All inputs are assumed validated (see main.py)."""
    z = np.asarray(observations, dtype=float)
    n, d = z.shape
    w = np.asarray(obs_weights, dtype=float)
    lam = float(smooth_weight)
    start = np.asarray(start, dtype=float)
    end = np.asarray(end, dtype=float)

    # --- Feasibility: separable per point, so check each corridor directly. ---
    for i, corr in enumerate(corridors):
        if corr.A.shape[0] == 0:
            continue
        if find_feasible(corr.A, corr.b) is None:
            return SmoothResult(
                "infeasible", False, None, None, None, 0,
                message=f"corridor {i} is empty (A x <= b has no solution)",
            )

    # Endpoints are pinned; they must satisfy their own corridors.
    for i, pt in ((0, start), (n - 1, end)):
        corr = corridors[i]
        if corr.A.shape[0] and float(np.max(corr.A @ pt - corr.b)) > 1e-7:
            return SmoothResult(
                "infeasible", False, None, None, None, 0,
                message=f"fixed endpoint {i} lies outside its corridor",
            )

    # --- Degenerate case: no interior points to optimise. ---
    if n == 2:
        path = np.vstack([start, end])
        obj = _objective(path, z, w, lam)
        primal = _primal_residual(path, corridors)
        return SmoothResult(
            "optimal", True, path, obj,
            residuals={
                "primal_infeasibility": primal,
                "stationarity": 0.0,
                "complementary_slackness": 0.0,
                "dual_infeasibility": 0.0,
            },
            iterations=0,
        )

    # --- Assemble the reduced QP over interior points x_1..x_{n-2}. ---
    # Full stacked objective:  (X-Z)^T W (X-Z) + lam * X^T (D2^T D2 kron I) X
    # with X in R^{n*d}; fixed blocks eliminated by substitution.
    D2 = np.zeros((n - 2, n))
    for i in range(n - 2):
        D2[i, i] = 1.0
        D2[i, i + 1] = -2.0
        D2[i, i + 2] = 1.0
    W = np.diag(w)
    H_full = 2.0 * (W + lam * (D2.T @ D2))
    H_full = np.kron(H_full, np.eye(d))
    g_full = -2.0 * np.kron(w, np.ones(d)) * z.reshape(-1)

    fixed_idx = np.concatenate([np.arange(d), np.arange((n - 1) * d, n * d)])
    free_idx = np.arange(d, (n - 1) * d)
    x_fixed = np.concatenate([start, end])

    H = H_full[np.ix_(free_idx, free_idx)]
    g = g_full[free_idx] + H_full[np.ix_(free_idx, fixed_idx)] @ x_fixed

    # Block-diagonal corridor constraints over interior points.
    blocks = [corridors[i] for i in range(1, n - 1)]
    rows = sum(c.A.shape[0] for c in blocks)
    k = (n - 2) * d
    A = np.zeros((rows, k))
    b = np.zeros(rows)
    r = 0
    for j, corr in enumerate(blocks):
        mi = corr.A.shape[0]
        if mi:
            A[r : r + mi, j * d : (j + 1) * d] = corr.A
            b[r : r + mi] = corr.b
            r += mi
    A = A[:r]
    b = b[:r]

    res = solve_qp(H, g, A, b, tol=tol)
    if res.status != "optimal":
        return SmoothResult(
            res.status, False, None, None, None, res.iterations,
            message=res.message or f"QP solver returned {res.status}",
        )

    path = np.empty((n, d))
    path[0] = start
    path[-1] = end
    path[1:-1] = res.x.reshape(n - 2, d)

    obj = _objective(path, z, w, lam)
    residuals = {
        "primal_infeasibility": _primal_residual(path, corridors),
        "stationarity": float(np.max(np.abs(H @ res.x + g + A.T @ res.lam))) if r else float(np.max(np.abs(H @ res.x + g))),
        "complementary_slackness": float(np.max(np.abs(res.lam * (A @ res.x - b)))) if r else 0.0,
        "dual_infeasibility": float(max(0.0, -np.min(res.lam))) if r else 0.0,
    }
    return SmoothResult("optimal", True, path, obj, residuals, res.iterations)


def _primal_residual(path: np.ndarray, corridors: list[Corridor]) -> float:
    worst = 0.0
    for x_i, corr in zip(path, corridors):
        if corr.A.shape[0]:
            worst = max(worst, float(np.max(corr.A @ x_i - corr.b)))
    return worst
