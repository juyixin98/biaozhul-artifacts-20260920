"""Primal active-set solver for small/medium strictly-convex quadratic programs.

Solves

    min_x  0.5 * x^T H x + g^T x
    s.t.   A x <= b

where H must be symmetric positive definite.  The algorithm is the
active-set method of Nocedal & Wright, "Numerical Optimization", Alg. 16.3.
A starting feasible point is obtained with HiGHS (scipy.optimize.linprog),
which doubles as the feasibility oracle: if the LP phase fails, the QP is
infeasible and that is reported instead of silently returning a bad point.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linprog

__all__ = ["QPResult", "find_feasible", "solve_qp"]


@dataclass
class QPResult:
    status: str  # "optimal" | "infeasible" | "iteration_limit" | "numerical_error"
    x: np.ndarray | None  # primal solution, None unless status == "optimal"
    lam: np.ndarray | None  # dual multipliers for A x <= b (>= 0), same length as b
    iterations: int
    message: str = ""
    objective: float | None = field(default=None)


def find_feasible(A: np.ndarray, b: np.ndarray) -> np.ndarray | None:
    """Return a point x with A x <= b, or None if the polytope is empty."""
    m, n = A.shape
    if m == 0:
        return np.zeros(n)
    res = linprog(
        c=np.zeros(n),
        A_ub=A,
        b_ub=b,
        bounds=(None, None),
        method="highs",
    )
    if res.status == 0:
        return res.x
    return None


def _solve_kkt(H: np.ndarray, rhs_grad: np.ndarray, A_w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Solve the equality-constrained subproblem KKT system.

    Returns (p, lam) solving
        [H  A_w^T] [p  ]   [rhs_grad]
        [A_w 0   ] [lam] = [   0    ]
    """
    n = H.shape[0]
    k = A_w.shape[0]
    if k == 0:
        return np.linalg.solve(H, rhs_grad), np.zeros(0)
    kkt = np.zeros((n + k, n + k))
    kkt[:n, :n] = H
    kkt[:n, n:] = A_w.T
    kkt[n:, :n] = A_w
    rhs = np.concatenate([rhs_grad, np.zeros(k)])
    try:
        sol = np.linalg.solve(kkt, rhs)
    except np.linalg.LinAlgError:
        # Degenerate (dependent) active constraints: fall back to the
        # minimum-norm least-squares solution rather than crashing.
        sol = np.linalg.lstsq(kkt, rhs, rcond=None)[0]
    return sol[:n], sol[n:]


def solve_qp(
    H: np.ndarray,
    g: np.ndarray,
    A: np.ndarray,
    b: np.ndarray,
    tol: float = 1e-9,
    max_iter: int = 2000,
) -> QPResult:
    """Solve the strictly-convex QP.  See module docstring for the form."""
    H = np.asarray(H, dtype=float)
    g = np.asarray(g, dtype=float)
    A = np.asarray(A, dtype=float).reshape(-1, H.shape[0])
    b = np.asarray(b, dtype=float).reshape(-1)
    n = H.shape[0]
    m = A.shape[0]

    def objective(x: np.ndarray) -> float:
        return float(0.5 * x @ H @ x + g @ x)

    if m == 0:
        x = np.linalg.solve(H, -g)
        return QPResult("optimal", x, np.zeros(0), 1, objective=objective(x))

    x = find_feasible(A, b)
    if x is None:
        return QPResult("infeasible", None, None, 0, message="constraint polytope is empty")

    active: list[int] = list(np.nonzero(A @ x >= b - 1e-8)[0])

    for it in range(1, max_iter + 1):
        A_w = A[active] if active else np.zeros((0, n))
        grad = H @ x + g
        p, lam = _solve_kkt(H, -grad, A_w)

        if np.linalg.norm(p, np.inf) <= tol:
            if lam.size == 0 or float(lam.min()) >= -tol:
                full_lam = np.zeros(m)
                if lam.size:
                    full_lam[np.asarray(active)] = np.maximum(lam, 0.0)
                return QPResult("optimal", x, full_lam, it, objective=objective(x))
            # Drop the constraint with the most negative multiplier.
            drop = int(np.argmin(lam))
            del active[drop]
            continue

        # Step length: largest alpha in [0, 1] keeping x + alpha*p feasible.
        alpha = 1.0
        blocking = None
        inactive_mask = np.ones(m, dtype=bool)
        if active:
            inactive_mask[np.asarray(active)] = False
        inactive_idx = np.nonzero(inactive_mask)[0]
        Ap = A[inactive_idx] @ p
        for j, i in enumerate(inactive_idx):
            if Ap[j] > tol:
                t = (b[i] - A[i] @ x) / Ap[j]
                if t < alpha - 1e-12:
                    alpha = max(float(t), 0.0)
                    blocking = int(i)
        x = x + alpha * p
        if blocking is not None:
            active.append(blocking)

    return QPResult(
        "iteration_limit",
        None,
        None,
        max_iter,
        message=f"active-set method did not converge in {max_iter} iterations",
    )
