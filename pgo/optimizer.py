"""SE(2) pose graph optimizer (robust, sparse Gauss-Newton / Levenberg-Marquardt).

Residual model
---------------
For an edge ``i -> j`` with relative measurement ``z = (z_xy, theta_z)`` the
error transform is

    E = Z^{-1} ( T_i^{-1} T_j )

and the (unwhitened) 3-vector residual is

    e = [ E.tx, E.ty, wrap(E.theta) ].

The angular component is wrapped to (-pi, pi], which keeps the residual small
near the optimum and is essential for a linearized solver to behave across
the angle branch cut.  Edges carry a 3x3 information matrix ``Omega``; the
whitened squared error is ``s = e^T Omega e`` and the edge objective is
``rho(s)`` for a (possibly robust) kernel.

Sparsity / gauge
-----------------
The per-edge Jacobian only touches the two incident 3-DOF blocks, so the
normal equations are assembled sparsously (scipy.sparse) and solved with a
sparse LU.  Node 0 is held fixed by default, which removes the 3 global
SE(2) gauge freedoms; arbitrary node sets can be fixed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import scipy.sparse as sp
import scipy.sparse.csgraph as csgraph
import scipy.sparse.linalg as spla

from .se2 import wrap_angle
from .kernels import kernel_scales


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class Edge:
    """Relative pose constraint between nodes ``i`` and ``j``.

    ``measurement`` is (dx, dy, dtheta) expressed in node i's frame;
    ``information`` is the 3x3 (symmetric positive definite) information
    matrix weighting (x, y, theta).
    """

    i: int
    j: int
    measurement: np.ndarray
    information: np.ndarray

    def __post_init__(self) -> None:
        self.measurement = np.asarray(self.measurement, dtype=float).reshape(3)
        self.information = np.asarray(self.information, dtype=float).reshape(3, 3)
        if self.i == self.j:
            raise ValueError(f"edge loop on a single node ({self.i}) is not supported")
        if not np.allclose(self.information, self.information.T, atol=1e-10):
            raise ValueError(f"edge {self.i}->{self.j}: information matrix not symmetric")
        eigvals = np.linalg.eigvalsh(0.5 * (self.information + self.information.T))
        if eigvals.min() <= 0.0:
            raise ValueError(
                f"edge {self.i}->{self.j}: information matrix not positive definite "
                f"(min eigenvalue {eigvals.min():.3e})"
            )


@dataclass
class PoseGraph:
    """A set of SE(2) nodes (initial poses) and relative constraints."""

    poses: np.ndarray  # (N, 3): x, y, theta
    edges: list[Edge]

    def __post_init__(self) -> None:
        self.poses = np.asarray(self.poses, dtype=float).reshape(-1, 3)
        n = self.poses.shape[0]
        if n < 1:
            raise ValueError("pose graph needs at least one node")
        for e in self.edges:
            if not (0 <= e.i < n and 0 <= e.j < n):
                raise ValueError(f"edge {e.i}->{e.j} references node outside [0, {n})")
        # keep all starting angles canonical
        self.poses[:, 2] = wrap_angle(self.poses[:, 2])

    @property
    def num_nodes(self) -> int:
        return self.poses.shape[0]


@dataclass
class OptimizeOptions:
    max_iterations: int = 30
    """Outer IRLS / LM iterations."""
    convergence_cost_tol: float = 1e-10
    convergence_step_tol: float = 1e-9
    gradient_tol: float = 1e-8
    kernel: str = "none"
    """one of 'none', 'huber', 'cauchy'."""
    kernel_delta: float = 1.0
    """Kernel threshold (whitened residual units); g2o convention."""
    lm_init: float = 1e-6
    """Initial Levenberg-Marquardt damping (0 ~ pure Gauss-Newton)."""
    lm_factor_up: float = 4.0
    lm_factor_down: float = 2.0
    lm_max: float = 1e10
    fixed_nodes: Optional[list[int]] = None
    """Nodes held fixed; defaults to [0] (pins the global SE(2) gauge)."""


@dataclass
class IterationRecord:
    iteration: int
    cost: float
    gradient_inf_norm: float
    step_inf_norm: float
    damping: float
    accepted: bool


@dataclass
class OptimizeResult:
    poses: np.ndarray
    initial_cost: float
    final_cost: float
    initial_gradient_inf_norm: float
    final_gradient_inf_norm: float
    iterations: int
    converged: str  # 'cost' | 'step' | 'gradient' | 'max_iterations' | 'no_descent'
    degenerate: bool
    connected_components: int
    min_eigenvalue: Optional[float]
    max_eigenvalue: Optional[float]
    edge_costs: np.ndarray  # robust cost contribution per edge, in input order
    history: list[IterationRecord] = field(default_factory=list)
    message: str = ""


# ---------------------------------------------------------------------------
# Residual and analytic Jacobians
# ---------------------------------------------------------------------------


def edge_residual(poses: np.ndarray, edge: Edge) -> np.ndarray:
    """Residual e = Log3(Z^{-1} (T_i^{-1} T_j)); angle wrapped to (-pi, pi]."""
    ti = poses[edge.i, :2]
    tj = poses[edge.j, :2]
    th_i = poses[edge.i, 2]
    th_j = poses[edge.j, 2]
    z_xy = edge.measurement[:2]
    th_z = edge.measurement[2]

    # w = R(-theta_i) (t_j - t_i) - z_xy is the relative translation before
    # rotating the error into the measurement frame.
    ci, si = np.cos(-th_i), np.sin(-th_i)
    ri = np.array([[ci, -si], [si, ci]])
    w = ri @ (tj - ti) - z_xy

    cz, sz = np.cos(-th_z), np.sin(-th_z)
    rz = np.array([[cz, -sz], [sz, cz]])
    e_xy = rz @ w
    e_th = wrap_angle(th_j - th_i - th_z)
    return np.array([e_xy[0], e_xy[1], e_th])


def edge_jacobians(poses: np.ndarray, edge: Edge) -> tuple[np.ndarray, np.ndarray]:
    """Analytic 3x3 Jacobians of the residual w.r.t. nodes i and j.

    Let  w = R(-theta_i)(t_j - t_i) - z_xy  and e_xy = R(-theta_z) w. Then:

        de_xy/dt_i = -R(-theta_z) R(-theta_i)
        de_xy/dtheta_i = R(-theta_z) (-J (w + z_xy))     [J = 90-degree rotation]
        de_xy/dt_j =  R(-theta_z) R(-theta_i)
        de_xy/dtheta_j = 0
        de_theta/dtheta_i = -1,  de_theta/dtheta_j = 1
    """
    ti = poses[edge.i, :2]
    tj = poses[edge.j, :2]
    th_i = poses[edge.i, 2]
    th_z = edge.measurement[2]

    ci, si = np.cos(-th_i), np.sin(-th_i)
    ri = np.array([[ci, -si], [si, ci]])
    # v = R(-theta_i)(t_j - t_i) = w + z_xy;  d w / d theta_i = -J v
    v = ri @ (tj - ti)

    cz, sz = np.cos(-th_z), np.sin(-th_z)
    rz = np.array([[cz, -sz], [sz, cz]])

    rzri = rz @ ri
    dxy_dthi = rz @ np.array([v[1], -v[0]])

    ai = np.zeros((3, 3))
    aj = np.zeros((3, 3))
    ai[:2, :2] = -rzri
    ai[:2, 2] = dxy_dthi
    ai[2, 2] = -1.0
    aj[:2, :2] = rzri
    aj[2, 2] = 1.0
    return ai, aj


def edge_residual_and_jacobians(
    poses: np.ndarray, edge: Edge
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Convenience: residual plus both Jacobians (shares the trig evaluation)."""
    return edge_residual(poses, edge), *edge_jacobians(poses, edge)


def numerical_jacobian(
    func, x0: np.ndarray, eps: float = 1e-7
) -> np.ndarray:
    """Central-difference Jacobian of ``func(x) -> (m,)`` at ``x0``."""
    x0 = np.asarray(x0, dtype=float)
    f0 = np.atleast_1d(np.asarray(func(x0), dtype=float))
    jac = np.zeros((f0.size, x0.size))
    for k in range(x0.size):
        xp = x0.copy()
        xm = x0.copy()
        xp[k] += eps
        xm[k] -= eps
        jac[:, k] = (np.atleast_1d(func(xp)) - np.atleast_1d(func(xm))) / (2.0 * eps)
    return jac


# ---------------------------------------------------------------------------
# Cost / normal-equations assembly
# ---------------------------------------------------------------------------


def _cost_and_edge_info(
    poses: np.ndarray, graph: PoseGraph, kernel: str, delta: float
):
    """Return (total robust cost, per-edge cost, per-edge residuals/weights)."""
    total = 0.0
    edge_costs = np.zeros(len(graph.edges))
    residuals = []
    weights = np.zeros(len(graph.edges))
    for k, e in enumerate(graph.edges):
        r = edge_residual(poses, e)
        s = float(r @ e.information @ r)
        kr = kernel_scales(s, kernel, delta)
        total += kr.value
        edge_costs[k] = kr.value
        residuals.append(r)
        weights[k] = kr.weight
    return total, edge_costs, residuals, weights


def _assemble(
    poses: np.ndarray, graph: PoseGraph, kernel: str, delta: float, fixed: set[int]
):
    """Build sparse normal equations H dx = b on the free variables.

    Uses IRLS weighting: edge block information is ``rho'(s) * Omega``.
    Returns (H, b, cost, edge_costs, grad_inf_norm).
    """
    n = graph.num_nodes
    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []
    b = np.zeros(3 * n)
    total_cost = 0.0
    edge_costs = np.zeros(len(graph.edges))

    def add_block(ar, ac, block):
        for rr in range(3):
            for cc in range(3):
                rows.append(ar + rr)
                cols.append(ac + cc)
                vals.append(float(block[rr, cc]))

    for k, e in enumerate(graph.edges):
        r = edge_residual(poses, e)
        ai, aj = edge_jacobians(poses, e)
        s = float(r @ e.information @ r)
        kr = kernel_scales(s, kernel, delta)
        total_cost += kr.value
        edge_costs[k] = kr.value
        w = kr.weight * e.information

        ii, jj = 3 * e.i, 3 * e.j
        add_block(ii, ii, ai.T @ w @ ai)
        add_block(ii, jj, ai.T @ w @ aj)
        add_block(jj, ii, aj.T @ w @ ai)
        add_block(jj, jj, aj.T @ w @ aj)
        b[ii:ii + 3] += ai.T @ w @ r
        b[jj:jj + 3] += aj.T @ w @ r

    H = sp.coo_matrix((vals, (rows, cols)), shape=(3 * n, 3 * n)).tocsr()
    grad_inf = float(np.max(np.abs(b))) if b.size else 0.0

    # eliminate fixed (gauge) variables
    free_mask = np.ones(3 * n, dtype=bool)
    for f in fixed:
        free_mask[3 * f:3 * f + 3] = False
    Hf = H[free_mask][:, free_mask]
    bf = b[free_mask]
    return Hf, bf, total_cost, edge_costs, float(np.max(np.abs(bf)) if bf.size else 0.0), free_mask


def _connected_components(graph: PoseGraph) -> int:
    n = graph.num_nodes
    if n == 1:
        return 1
    r, c = [], []
    for e in graph.edges:
        r += [e.i, e.j]
        c += [e.j, e.i]
    adj = sp.coo_matrix((np.ones(len(r)), (r, c)), shape=(n, n))
    return int(csgraph.connected_components(adj, directed=False, return_labels=False))


def _spectrum(Hf: sp.csr_matrix):
    """Smallest / largest algebraic eigenvalues of the free normal matrix.

    Returns (None, None) if they cannot be estimated (e.g. empty matrix).
    """
    m = Hf.shape[0]
    if m == 0:
        return None, None
    Hf = (Hf + Hf.T) * 0.5
    try:
        if m <= 50:
            eigvals = np.linalg.eigvalsh(Hf.toarray())
            return float(eigvals[0]), float(eigvals[-1])
        k = min(1, m - 2)
        lam_min = spla.eigsh(Hf, k=k, which="SA", return_eigenvectors=False)
        lam_max = spla.eigsh(Hf, k=k, which="LA", return_eigenvectors=False)
        return float(lam_min.min()), float(lam_max.max())
    except spla.ArpackNoConvergence:
        return None, None


# ---------------------------------------------------------------------------
# Optimization
# ---------------------------------------------------------------------------


def optimize(
    graph: PoseGraph, options: Optional[OptimizeOptions] = None
) -> OptimizeResult:
    """Run robust sparse LM/GN on ``graph``; the input graph is not modified."""
    options = options or OptimizeOptions()
    n = graph.num_nodes
    fixed = set(options.fixed_nodes if options.fixed_nodes is not None else [0])
    for f in fixed:
        if not 0 <= f < n:
            raise ValueError(f"fixed node {f} outside [0, {n})")
    if not fixed and n > 0:
        # with no anchor the problem has a 3-dimensional gauge null space
        pass

    poses = graph.poses.copy()

    initial_cost, _, _, _ = _cost_and_edge_info(
        poses, graph, options.kernel, options.kernel_delta
    )

    (Hf0, bf0, _, _, initial_grad, _) = _assemble(
        poses, graph, options.kernel, options.kernel_delta, fixed
    )

    lam = max(options.lm_init, 0.0)
    history: list[IterationRecord] = []
    converged = "max_iterations"
    message = ""

    for it in range(1, options.max_iterations + 1):
        Hf, bf, cost, edge_costs, grad_inf, free_mask = _assemble(
            poses, graph, options.kernel, options.kernel_delta, fixed
        )

        if grad_inf <= options.gradient_tol:
            converged = "gradient"
            message = f"gradient inf-norm {grad_inf:.3e} <= tol {options.gradient_tol:.1e}"
            history.append(
                IterationRecord(it, cost, grad_inf, 0.0, lam, True)
            )
            break

        # LM step with adaptive damping; H is (numerically) PSD.
        # Normal equations: (H + lam I) dx = -g, with g = J^T W e.
        step = None
        lam_try = lam
        for _inner in range(60):
            A = Hf + sp.eye(Hf.shape[0], format="csr") * lam_try
            try:
                dx_free = spla.spsolve(A.tocsc(), -bf)
            except (spla.MatrixRankWarning, RuntimeError, ValueError):
                dx_free = None
            if dx_free is not None and np.all(np.isfinite(dx_free)):
                step = dx_free
                lam = lam_try
                break
            lam_try = max(lam_try * options.lm_factor_up, 1e-12)
            if lam_try > options.lm_max:
                break
        if step is None:
            converged = "no_descent"
            message = "normal equations singular at every damping level"
            history.append(IterationRecord(it, cost, grad_inf, np.inf, lam, False))
            break

        candidate = poses.copy()
        candidate.flat[free_mask] += step
        candidate[:, 2] = wrap_angle(candidate[:, 2])
        new_cost, _, _, _ = _cost_and_edge_info(
            candidate, graph, options.kernel, options.kernel_delta
        )

        step_inf = float(np.max(np.abs(step))) if step.size else 0.0
        if new_cost < cost and np.isfinite(new_cost):
            rel_drop = (cost - new_cost) / max(1.0, cost)
            poses = candidate
            lam = max(lam / options.lm_factor_down, 0.0 if options.lm_init == 0 else 1e-12)
            history.append(IterationRecord(it, new_cost, grad_inf, step_inf, lam, True))
            if rel_drop < options.convergence_cost_tol:
                converged = "cost"
                message = (
                    f"relative cost decrease {rel_drop:.3e} < {options.convergence_cost_tol:.0e}"
                )
                break
            if step_inf < options.convergence_step_tol:
                converged = "step"
                message = f"step inf-norm {step_inf:.3e} < {options.convergence_step_tol:.0e}"
                break
        else:
            # reject, increase damping
            lam = max(lam_try * options.lm_factor_up, 1e-10)
            history.append(IterationRecord(it, cost, grad_inf, step_inf, lam, False))
            if lam > options.lm_max or (
                len(history) >= 3 and all(not h.accepted for h in history[-3:])
                and step_inf < 1e-12
            ):
                converged = "no_descent"
                message = "no cost-reducing step could be found (local minimum or infeasible fit)"
                break

    # final evaluation + degeneracy diagnostics
    final_cost, edge_costs, _, _ = _cost_and_edge_info(
        poses, graph, options.kernel, options.kernel_delta
    )
    Hf, _, _, _, final_grad, _ = _assemble(
        poses, graph, options.kernel, options.kernel_delta, fixed
    )
    n_comp = _connected_components(graph)
    lam_min, lam_max = _spectrum(Hf)
    degenerate = False
    reasons = []
    if n_comp > 1:
        degenerate = True
        reasons.append(f"{n_comp} connected components (unanchored gauge freedoms)")
    if lam_min is not None and lam_max is not None:
        if lam_min <= 1e-9 * max(1.0, lam_max):
            degenerate = True
            reasons.append(
                f"smallest normal-matrix eigenvalue {lam_min:.3e} "
                f"(max {lam_max:.3e})"
            )
    if degenerate and not message:
        message = "; ".join(reasons)

    return OptimizeResult(
        poses=poses,
        initial_cost=initial_cost,
        final_cost=final_cost,
        initial_gradient_inf_norm=initial_grad,
        final_gradient_inf_norm=final_grad,
        iterations=history[-1].iteration if history else 0,
        converged=converged,
        degenerate=degenerate,
        connected_components=n_comp,
        min_eigenvalue=lam_min,
        max_eigenvalue=lam_max,
        edge_costs=edge_costs,
        history=history,
        message=message,
    )
