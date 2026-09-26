"""Gauss-Newton / Levenberg-Marquardt solver for SE2 pose graphs.

The objective is the (optionally robust) sum of squared Mahalanobis residuals

    F(x) = sum_e rho_e( e_e(x).T @ Omega_e @ e_e(x) )

solved by iterative linearization.  Robust kernels enter through IRLS: at each
outer iteration every edge gets weight ``w_e = rho'(s_e)`` multiplying its
information matrix; an inner LM loop adapts damping and rejects steps that do
not decrease the robust cost.

Gauge freedom is removed by fixing poses.  Components without a fixed node are
automatically anchored on their smallest node (and reported) unless
``strict_anchoring`` is enabled.
"""

from dataclasses import dataclass, field

import numpy as np

from .graph import GraphStructureError, PoseGraph
from .residual import edge_residual, edge_residual_and_jacobians
from .robust import KERNEL_TYPES, robust_cost, robust_weight
from .se2 import wrap_angle

# LM damping adaptation factors.
_LAMBDA_UP = 10.0
_LAMBDA_DOWN = 0.5
_LAMBDA_MAX = 1.0e12


@dataclass
class OptimizeOptions:
    """Tuning knobs for :func:`optimize`."""

    max_iterations: int = 100
    ftol: float = 1.0e-8
    xtol: float = 1.0e-10
    gtol: float = 1.0e-8
    lambda_init: float = 1.0e-3
    strict_anchoring: bool = False


@dataclass
class EdgeStat:
    """Per-edge diagnostics recorded before and after optimization."""

    i: int
    j: int
    label: str
    kernel_type: str
    squared_residual_initial: float
    squared_residual_final: float
    robust_cost_initial: float
    robust_cost_final: float
    angular_residual_abs_final: float


@dataclass
class OptimizeResult:
    """Result bundle returned by :func:`optimize`."""

    poses: dict
    cost_initial: float
    cost_final: float
    cost_history: list
    iterations: int
    converged: bool
    termination_reason: str
    fixed_nodes: list
    auto_anchored_nodes: list
    connectivity: dict
    edge_stats: list = field(default_factory=list)
    max_angular_residual_initial: float = 0.0
    max_angular_residual_final: float = 0.0
    rms_residual_initial: float = 0.0
    rms_residual_final: float = 0.0


def _check_graph(graph: PoseGraph):
    if graph.num_nodes() == 0:
        raise GraphStructureError("pose graph has no nodes")
    for e in graph.edges:
        if e.kernel_type not in KERNEL_TYPES:
            raise GraphStructureError(
                f"edge {e.i}->{e.j}: unknown kernel {e.kernel_type!r}"
            )


def _evaluate_costs(poses, pos, edges):
    """Return (total robust cost, per-edge squared Mahalanobis distances)."""
    total = 0.0
    sq = np.empty(len(edges))
    for k, e in enumerate(edges):
        r = edge_residual(poses[pos[e.i]], poses[pos[e.j]], e.z)
        s = float(r @ e.information @ r)
        sq[k] = s
        total += robust_cost(e.kernel_type, s, e.kernel_parameter)
    return total, sq


def _build_linear_system(poses, pos, edges):
    """Build the damped-free normal equations ``H dx = b`` with ``b = -J'e``."""
    n = poses.shape[0]
    h = np.zeros((3 * n, 3 * n))
    b = np.zeros(3 * n)
    for e in edges:
        pi, pj = pos[e.i], pos[e.j]
        r, a, jb = edge_residual_and_jacobians(poses[pi], poses[pj], e.z)
        s = float(r @ e.information @ r)
        w = robust_weight(e.kernel_type, s, e.kernel_parameter)
        omega_w = w * e.information
        at_o = a.T @ omega_w
        bt_o = jb.T @ omega_w
        h[3 * pi : 3 * pi + 3, 3 * pi : 3 * pi + 3] += at_o @ a
        h[3 * pi : 3 * pi + 3, 3 * pj : 3 * pj + 3] += at_o @ jb
        h[3 * pj : 3 * pj + 3, 3 * pi : 3 * pi + 3] += bt_o @ a
        h[3 * pj : 3 * pj + 3, 3 * pj : 3 * pj + 3] += bt_o @ jb
        b[3 * pi : 3 * pi + 3] += at_o @ r
        b[3 * pj : 3 * pj + 3] += bt_o @ r
    return h, -b


def optimize(graph: PoseGraph, options: OptimizeOptions | None = None) -> OptimizeResult:
    """Run LM/IRLS nonlinear least squares on *graph*.

    The result is a *local* optimum: no global-optimality guarantee is made.
    """
    options = options or OptimizeOptions()
    _check_graph(graph)

    connectivity = graph.diagnose()
    unanchored = [c["member_nodes"] for c in connectivity["components"] if not c["fixed_nodes"]]
    if options.strict_anchoring and unanchored:
        raise GraphStructureError(
            "strict_anchoring: "
            f"{len(unanchored)} component(s) without a fixed node: {unanchored}"
        )
    auto_anchored = sorted(members[0] for members in unanchored)
    effective_fixed = sorted(set(graph.fixed_nodes) | set(auto_anchored))

    order = graph.ordered_indices()
    pos = {idx: k for k, idx in enumerate(order)}
    poses = np.array([graph.get_node(idx).initial_pose for idx in order], dtype=float)
    edges = graph.edges
    n = poses.shape[0]

    fixed_blocks = sorted({pos[idx] for idx in effective_fixed})
    free_mask = np.ones(3 * n, dtype=bool)
    for blk in fixed_blocks:
        free_mask[3 * blk : 3 * blk + 3] = False
    fixed_diag = np.zeros(3 * n)
    fixed_diag[~free_mask] = 1.0

    cost_initial, sq_initial = _evaluate_costs(poses, pos, edges)

    def angular_stats(sq, poses):
        # Recompute angular residuals for reporting (signed, wrapped).
        ang = [
            abs(edge_residual(poses[pos[e.i]], poses[pos[e.j]], e.z)[2])
            for e in edges
        ]
        return max(ang, default=0.0)

    cost = cost_initial
    cost_history = [cost]
    lam = float(options.lambda_init)
    converged = False
    reason = "max_iterations_reached"
    iterations = 0

    for iterations in range(1, options.max_iterations + 1):
        h_full, b_full = _build_linear_system(poses, pos, edges)

        # Gradient norm on free DOFs (b_full = -g, so g = -b_full).
        grad_norm = float(np.max(np.abs(b_full[free_mask]))) if free_mask.any() else 0.0
        if grad_norm < options.gtol:
            reason = "gradient_below_gtol"
            converged = True
            iterations_done = iterations - 1
            break

        step_accepted = False
        lam_trial = lam
        while lam_trial < _LAMBDA_MAX:
            h_trial = h_full.copy()
            diag = np.diag(h_trial).copy()
            # Classic LM damping on free DOFs only.
            damped = np.where(free_mask, diag * (1.0 + lam_trial), 1.0)
            np.fill_diagonal(h_trial, damped)
            # Zero fixed rows/columns and pin fixed poses through rhs = 0.
            h_trial[:, ~free_mask] = 0.0
            h_trial[~free_mask, :] = 0.0
            np.fill_diagonal(h_trial, np.where(free_mask, np.diag(h_trial), 1.0))
            b_trial = b_full.copy()
            b_trial[~free_mask] = 0.0

            try:
                dx = np.linalg.solve(h_trial, b_trial)
            except np.linalg.LinAlgError:
                lam_trial *= _LAMBDA_UP
                continue

            step_norm = float(np.linalg.norm(dx[free_mask]))
            candidate = poses + dx.reshape(n, 3)
            candidate[:, 2] = wrap_angle(candidate[:, 2])
            # Fixed poses must stay exactly unchanged.
            for blk in fixed_blocks:
                candidate[blk] = poses[blk]

            cost_new, _ = _evaluate_costs(candidate, pos, edges)
            if np.isfinite(cost_new) and cost_new < cost:
                poses = candidate
                cost = cost_new
                lam = max(lam_trial * _LAMBDA_DOWN, 1.0e-12)
                step_accepted = True
                break
            lam_trial *= _LAMBDA_UP

        cost_history.append(cost)

        if not step_accepted:
            reason = "no_step_accepted_damping_limit"
            converged = False
            iterations_done = iterations
            break

        rel_decrease = (cost_history[-2] - cost) / max(1.0, abs(cost_history[-2]))
        iterations_done = iterations

        if step_norm < options.xtol:
            converged = True
            reason = "step_size_below_xtol"
            break
        if abs(rel_decrease) < options.ftol:
            converged = True
            reason = "relative_cost_change_below_ftol"
            break
        if grad_norm < options.gtol:
            converged = True
            reason = "gradient_below_gtol"
            break
    else:
        iterations_done = options.max_iterations

    cost_final, sq_final = _evaluate_costs(poses, pos, edges)

    edge_stats = []
    for e, s0, s1 in zip(edges, sq_initial, sq_final):
        r1 = edge_residual(poses[pos[e.i]], poses[pos[e.j]], e.z)
        edge_stats.append(
            EdgeStat(
                i=e.i,
                j=e.j,
                label=e.label,
                kernel_type=e.kernel_type,
                squared_residual_initial=float(s0),
                squared_residual_final=float(s1),
                robust_cost_initial=float(
                    robust_cost(e.kernel_type, s0, e.kernel_parameter)
                ),
                robust_cost_final=float(
                    robust_cost(e.kernel_type, s1, e.kernel_parameter)
                ),
                angular_residual_abs_final=float(abs(r1[2])),
            )
        )

    rms = lambda sq: float(np.sqrt(np.mean(sq))) if len(sq) else 0.0
    return OptimizeResult(
        poses={idx: poses[pos[idx]].copy() for idx in order},
        cost_initial=float(cost_initial),
        cost_final=float(cost_final),
        cost_history=[float(c) for c in cost_history],
        iterations=iterations_done,
        converged=converged,
        termination_reason=reason,
        fixed_nodes=sorted(graph.fixed_nodes),
        auto_anchored_nodes=auto_anchored,
        connectivity=connectivity,
        edge_stats=edge_stats,
        max_angular_residual_initial=angular_stats(sq_initial, np.array(
            [graph.get_node(idx).initial_pose for idx in order]
        )),
        max_angular_residual_final=angular_stats(sq_final, poses),
        rms_residual_initial=rms(sq_initial),
        rms_residual_final=rms(sq_final),
    )
