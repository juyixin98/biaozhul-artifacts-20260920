"""Sparse Gauss-Newton optimizer for SE(2) pose graphs.

Error model
-----------
For an edge ``(i, j)`` with measured relative pose ``z`` the predicted
measurement is ``h = x_i^-1 (+) x_j`` and the error vector is

    e = z^-1 (+) h   (a 3-vector: translation expressed in the z-frame,
                      rotation angle normalized to [-pi, pi))

Each edge contributes a robustified cost ``0.5 * rho(e^T Omega e)`` where
``Omega`` is the 3x3 information matrix and ``rho`` is either the identity
(no kernel) or the Huber kernel.

The normal equations ``H dx = -b`` are assembled as a sparse matrix and
solved with a sparse direct solver.  One node (default: node 0) is held
fixed to remove the gauge freedom of the graph.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import numpy as np
import scipy.sparse
import scipy.sparse.linalg

from .se2 import wrap_angle

# ---------------------------------------------------------------------------
# Error function and analytic Jacobians
# ---------------------------------------------------------------------------


def edge_error_and_jacobians(
    xi: np.ndarray, xj: np.ndarray, z: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Error vector and analytic Jacobians for one relative-pose constraint.

    Parameters
    ----------
    xi, xj:
        Poses of nodes ``i`` and ``j`` (shape ``(3,)``).
    z:
        Measured relative pose from ``i`` to ``j`` (shape ``(3,)``).

    Returns
    -------
    e:
        Error 3-vector ``z^-1 (+) (xi^-1 (+) xj)`` with normalized angle.
    A:
        ``(3, 3)`` Jacobian ``d e / d xi``.
    B:
        ``(3, 3)`` Jacobian ``d e / d xj``.
    """
    d = xj[:2] - xi[:2]
    ci, si = np.cos(xi[2]), np.sin(xi[2])
    cz, sz = np.cos(z[2]), np.sin(z[2])

    # R_i^T and R_z^T (transposed rotation matrices).
    RiT = np.array([[ci, si], [-si, ci]])
    RzT = np.array([[cz, sz], [-sz, cz]])

    # Predicted relative pose h = xi^-1 (+) xj.
    h_trans = RiT @ d
    h_theta = wrap_angle(xj[2] - xi[2])

    # Error e = z^-1 (+) h.
    e_trans = RzT @ (h_trans - z[:2])
    e_theta = wrap_angle(h_theta - z[2])
    e = np.array([e_trans[0], e_trans[1], e_theta])

    # Jacobians (see e.g. Grisetti et al., "A Tutorial on Graph-Based SLAM").
    M = RzT @ RiT  # d e_trans / d t_j  (and negated for t_i)
    dRiT = np.array([[-si, ci], [-ci, -si]])  # d(R_i^T)/d theta_i
    de_dtheta_i = RzT @ dRiT @ d

    A = np.zeros((3, 3))
    A[:2, :2] = -M
    A[:2, 2] = de_dtheta_i
    A[2, 2] = -1.0

    B = np.zeros((3, 3))
    B[:2, :2] = M
    B[2, 2] = 1.0

    return e, A, B


# ---------------------------------------------------------------------------
# Robust kernel
# ---------------------------------------------------------------------------


def huber_rho(s: float, delta: float) -> float:
    """Huber cost ``rho(s)`` for squared norm ``s``."""
    if s <= delta * delta:
        return s
    return 2.0 * delta * np.sqrt(s) - delta * delta


def huber_weight(s: float, delta: float) -> float:
    """IRLS weight ``rho'(s)`` of the Huber kernel."""
    if s <= delta * delta:
        return 1.0
    return delta / np.sqrt(s)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Edge:
    """One relative-pose constraint between nodes ``i`` and ``j``."""

    i: int
    j: int
    z: np.ndarray  # measured relative pose, shape (3,)
    omega: np.ndarray  # 3x3 information matrix (symmetric positive definite)


@dataclass
class OptimizeOptions:
    max_iterations: int = 50
    robust_kernel: str = "huber"  # "none" | "huber"
    huber_delta: float = 1.0
    gradient_tolerance: float = 1e-6
    cost_tolerance: float = 1e-9
    fix_node: int = 0  # node held fixed to remove the gauge freedom
    degeneracy_threshold: float = 1e-9


@dataclass
class OptimizeResult:
    poses: np.ndarray  # (N, 3) optimized poses
    converged: bool
    iterations: int
    cost_history: list[float] = field(default_factory=list)
    gradient_norm_history: list[float] = field(default_factory=list)
    final_cost: float = 0.0
    final_gradient_norm: float = 0.0
    degeneracy: dict = field(default_factory=dict)
    message: str = ""


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _edge_weight(s: float, options: OptimizeOptions) -> float:
    if options.robust_kernel == "huber":
        return huber_weight(s, options.huber_delta)
    return 1.0


def _edge_cost(s: float, options: OptimizeOptions) -> float:
    if options.robust_kernel == "huber":
        return 0.5 * huber_rho(s, options.huber_delta)
    return 0.5 * s


def _total_cost(
    poses: np.ndarray, edges: list[Edge], options: OptimizeOptions
) -> float:
    total = 0.0
    for edge in edges:
        e, _, _ = edge_error_and_jacobians(poses[edge.i], poses[edge.j], edge.z)
        s = float(e @ edge.omega @ e)
        total += _edge_cost(s, options)
    return total


def _linearize(
    poses: np.ndarray, edges: list[Edge], options: OptimizeOptions
) -> tuple[scipy.sparse.lil_matrix, np.ndarray, float]:
    """Assemble the robustified normal equations H dx = -b."""
    n = poses.shape[0]
    H = scipy.sparse.lil_matrix((3 * n, 3 * n))
    b = np.zeros(3 * n)
    cost = 0.0
    for edge in edges:
        e, A, B = edge_error_and_jacobians(poses[edge.i], poses[edge.j], edge.z)
        omega = edge.omega
        s = float(e @ omega @ e)
        w = _edge_weight(s, options)
        cost += _edge_cost(s, options)

        omega_A = w * (omega @ A)
        omega_B = w * (omega @ B)
        ii = slice(3 * edge.i, 3 * edge.i + 3)
        jj = slice(3 * edge.j, 3 * edge.j + 3)
        H[ii, ii] += A.T @ omega_A
        H[ii, jj] += A.T @ omega_B
        H[jj, ii] += B.T @ omega_A
        H[jj, jj] += B.T @ omega_B
        b[ii] += A.T @ (w * (omega @ e))
        b[jj] += B.T @ (w * (omega @ e))
    return H, b, cost


def _anchor(H: scipy.sparse.lil_matrix, b: np.ndarray, node: int) -> None:
    """Hold ``node`` fixed: replace its block rows/cols with identity."""
    sl = slice(3 * node, 3 * node + 3)
    H[sl, :] = 0.0
    H[:, sl] = 0.0
    H[sl, sl] = np.eye(3)
    b[sl] = 0.0


def _analyze_degeneracy(
    H: scipy.sparse.spmatrix, threshold: float
) -> dict:
    """Eigen-analysis of the anchored Hessian (gauge already fixed)."""
    n = H.shape[0]
    Hcsc = H.tocsc()
    if n <= 1200:
        eigvals = np.linalg.eigvalsh(Hcsc.toarray())
    else:
        try:
            smallest = scipy.sparse.linalg.eigsh(Hcsc, k=3, which="SA")[0]
            largest = scipy.sparse.linalg.eigsh(Hcsc, k=1, which="LA")[0]
            eigvals = np.concatenate([smallest, largest])
        except Exception:
            return {
                "is_degenerate": True,
                "min_eigenvalue": None,
                "max_eigenvalue": None,
                "condition_estimate": None,
                "note": "eigen-analysis failed; Hessian treated as degenerate",
            }
    min_eig = float(np.min(eigvals))
    max_eig = float(np.max(eigvals))
    cond = float(max_eig / min_eig) if min_eig > 0 else float("inf")
    is_degenerate = min_eig < threshold
    note = (
        "Hessian is (near-)singular: the graph has unobservable directions "
        "(e.g. disconnected components or insufficient constraints)."
        if is_degenerate
        else "Hessian is well conditioned; no degeneracy detected."
    )
    return {
        "is_degenerate": bool(is_degenerate),
        "min_eigenvalue": min_eig,
        "max_eigenvalue": max_eig,
        "condition_estimate": cond,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def optimize(
    initial_poses: np.ndarray,
    edges: list[Edge],
    options: OptimizeOptions | None = None,
) -> OptimizeResult:
    """Optimize a pose graph from ``initial_poses`` given relative ``edges``.

    Parameters
    ----------
    initial_poses:
        ``(N, 3)`` array of initial pose guesses.
    edges:
        Relative-pose constraints.
    options:
        Solver options; see :class:`OptimizeOptions`.
    """
    options = options or OptimizeOptions()
    poses = np.asarray(initial_poses, dtype=float).copy()
    n = poses.shape[0]
    if not (0 <= options.fix_node < n):
        raise ValueError(f"fix_node {options.fix_node} out of range [0, {n})")
    if options.robust_kernel not in ("none", "huber"):
        raise ValueError(f"unknown robust_kernel: {options.robust_kernel!r}")

    cost_history: list[float] = []
    grad_history: list[float] = []
    converged = False
    message = "maximum iterations reached"
    H_final: scipy.sparse.spmatrix | None = None
    iterations = 0

    for it in range(options.max_iterations):
        iterations = it + 1
        H, b, cost = _linearize(poses, edges, options)
        _anchor(H, b, options.fix_node)
        H = H.tocsc()
        H_final = H

        grad_norm = float(np.max(np.abs(b))) if b.size else 0.0
        cost_history.append(cost)
        grad_history.append(grad_norm)

        if grad_norm < options.gradient_tolerance:
            converged = True
            message = "converged: gradient norm below tolerance"
            break

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                dx = scipy.sparse.linalg.spsolve(H, -b)
            except Exception:
                dx = None
        if dx is None or not np.all(np.isfinite(dx)):
            message = "linear solve failed (singular Hessian); aborting"
            break
        if any(
            "MatrixRankWarning" in str(w.category.__name__)
            or "ill-conditioned" in str(w.message).lower()
            for w in caught
        ):
            message = "linear solve reported a rank-deficient Hessian; aborting"
            break

        # Backtracking line search on the Gauss-Newton step.
        step = dx.reshape(n, 3)
        accepted = False
        alpha = 1.0
        for _ in range(8):
            trial = poses + alpha * step
            trial[:, 2] = wrap_angle(trial[:, 2])
            trial_cost = _total_cost(trial, edges, options)
            if trial_cost < cost:
                accepted = True
                break
            alpha *= 0.5
        if not accepted:
            message = "no cost-decreasing step found; stopping"
            break

        rel_decrease = (cost - trial_cost) / max(abs(cost), 1e-12)
        poses = trial
        if rel_decrease < options.cost_tolerance:
            converged = True
            message = "converged: relative cost decrease below tolerance"
            # Update the last history entries to the final (optimal) state so
            # histories always end at the reported solution.
            H, b, cost = _linearize(poses, edges, options)
            _anchor(H, b, options.fix_node)
            H_final = H.tocsc()
            cost_history[-1] = cost
            grad_history[-1] = float(np.max(np.abs(b))) if b.size else 0.0
            break

    degeneracy = (
        _analyze_degeneracy(H_final, options.degeneracy_threshold)
        if H_final is not None
        else {"is_degenerate": True, "note": "no iteration was performed"}
    )

    return OptimizeResult(
        poses=poses,
        converged=converged,
        iterations=iterations,
        cost_history=cost_history,
        gradient_norm_history=grad_history,
        final_cost=cost_history[-1] if cost_history else float("nan"),
        final_gradient_norm=grad_history[-1] if grad_history else float("nan"),
        degeneracy=degeneracy,
        message=message,
    )
