"""Constrained 2D trajectory smoothing via SLSQP.

The optimizer moves *interior* polyline vertices (endpoints are fixed) while
minimizing a weighted objective that trades off:

* deviation from the original vertices,
* integrated bending (sum of squared curvature, via the Menger curvature of
  each consecutive vertex triple),
* jerk (squared third differences).

Hard constraints:

* disk corridor around every original interior vertex (deviation cap),
* curvature cap per vertex triple (Menger curvature, exact),
* minimum edge length (prevents fold-back degeneracy),
* signed-distance clearance to every rectangle at every control vertex AND
  every control-segment midpoint.

The final trajectory is then independently and densely verified
(:func:`app.geometry.verify_trajectory`) — adaptively subdividing every
segment and additionally running an exact segment/rectangle test.  Any
infeasibility, non-convergence, timeout, or post-solve collision causes the
service to return the *original* path with a failure status; a freshly
optimized path is published only when every gate passes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize

from .geometry import (
    Rect,
    collapse_consecutive_duplicates,
    densify,
    expand_to_groups,
    rect_sdf_gradients,
    rect_signed_distance,
    segment_rect_distance,
)

MAX_POINTS_HARD_LIMIT = 100
MAX_SUBDIVISIONS_PER_EDGE = 200


@dataclass
class SmoothConfig:
    max_iterations: int = 200
    timeout_seconds: float = 30.0
    ftol: float = 1e-10
    # Physical-unit tunables; None => solver chooses a problem-scaled default.
    curvature_cap: float | None = None          # 1 / length-unit
    corridor: float | None = None               # length-units
    clearance: float = 0.0                      # length-units
    min_edge_length: float | None = None        # length-units
    deviation_weight: float = 1.0
    bend_weight: float = 0.0
    jerk_weight: float = 0.05
    default_corridor_fraction: float = 0.25     # * bbox diagonal
    optimize_midpoint_clearance: bool = True


@dataclass
class SmoothResult:
    status: str
    message: str
    points: np.ndarray
    converged: bool = False
    iterations: int = 0
    objective: float = 0.0
    objective_components: dict = field(default_factory=dict)
    residuals: dict = field(default_factory=dict)
    verification: dict = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    n_control_points: int = 0
    n_variables: int = 0
    scale: float = 1.0
    parameters: dict = field(default_factory=dict)
    collapsed_duplicates: int = 0


# ---------------------------------------------------------------------------
# Scale normalization — the same problem expressed at mm vs km scale must be
# solved identically.  All optimization happens in normalized coordinates
# where the bounding-box diagonal of (path + obstacles) is exactly 1.
# ---------------------------------------------------------------------------

def _problem_scale(points: np.ndarray, rects: list[Rect]) -> float:
    xs, ys = list(points[:, 0]), list(points[:, 1])
    for r in rects:
        xs += [r.xmin, r.xmax]
        ys += [r.ymin, r.ymax]
    diag = float(np.hypot(np.max(xs) - np.min(xs), np.max(ys) - np.min(ys)))
    if not np.isfinite(diag) or diag <= 1e-12:
        return 1.0
    return diag


# ---------------------------------------------------------------------------
# Menger curvature with analytic gradients
# ---------------------------------------------------------------------------

def _menger_curvature_and_jac(P: np.ndarray):
    """Discrete curvature at every interior vertex and its dense Jacobian.

    Returns (kappa, J) with kappa shape (m-2,) and J shape (m-2, 2m)
    (column order: x0,y0,x1,y1,...).  kappa is unsigned (absolute).
    """
    p0 = P[:-2]
    p1 = P[1:-1]
    p2 = P[2:]
    a = p1 - p0                      # edge i-1
    b = p2 - p1                      # edge i
    la = np.hypot(a[:, 0], a[:, 1])
    lb = np.hypot(b[:, 0], b[:, 1])
    c = p2 - p0                      # a + b
    dc = np.hypot(c[:, 0], c[:, 1])
    cross = a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]
    L = la * lb
    den = L * dc
    safe = den > 1e-30
    kappa = np.zeros(len(P) - 2)
    kappa[safe] = np.abs(2.0 * cross[safe] / den[safe])

    m = len(P)
    J = np.zeros((m - 2, 2 * m))
    if not np.any(safe):
        return kappa, J

    a, b, c = a[safe], b[safe], c[safe]
    la, lb, dc = la[safe], lb[safe], dc[safe]
    cross, L, den = cross[safe], L[safe], den[safe]
    # Gradient of |cross|: sign(cross).  Exactly-collinear triples have zero
    # cross (and a true subgradient containing 0), so leaving the sign at 0
    # gives a zero gradient there — forcing it to +1 would actively steer the
    # SQP in a wrong direction at straight segments.
    sign = np.sign(cross)[:, None]

    # d kappa / d a and d kappa / d b (kappa = |2 cross / den|).
    factor = (-2.0 * cross / den**2)[:, None] * sign
    two_over_den = (2.0 / den)[:, None] * sign
    d_cross_a = np.column_stack([b[:, 1], -b[:, 0]])
    d_cross_b = np.column_stack([-a[:, 1], a[:, 0]])
    d_den_a = (dc * (lb / la))[:, None] * a + (L / dc)[:, None] * c
    d_den_b = (dc * (la / lb))[:, None] * b + (L / dc)[:, None] * c
    dk_a = two_over_den * d_cross_a + factor * d_den_a
    dk_b = two_over_den * d_cross_b + factor * d_den_b

    rows = np.where(safe)[0]
    for slot, g in ((0, -dk_a), (1, dk_a - dk_b), (2, dk_b)):
        idx = rows + slot
        J[rows, 2 * idx] = g[:, 0]
        J[rows, 2 * idx + 1] = g[:, 1]
    return kappa, J


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def smooth_path(raw_points: np.ndarray, raw_rects: list[Rect], cfg: SmoothConfig) -> SmoothResult:
    start = time.monotonic()
    original = np.asarray(raw_points, dtype=float).copy()
    result = SmoothResult(
        status="error", message="", points=original,
        n_control_points=len(original),
    )
    if len(original) < 2:
        result.status = "invalid_input"
        result.message = "path must contain at least 2 points"
        result.elapsed_seconds = time.monotonic() - start
        return result

    # Duplicate consecutive points collapse the optimizer's edge-length
    # calculations; collapse them, then re-expand to the original indexing.
    P0_all, groups = collapse_consecutive_duplicates(original)
    result.collapsed_duplicates = len(original) - len(P0_all)
    P0 = P0_all
    m = len(P0)
    result.n_control_points = m

    if m < 2:
        result.status = "invalid_input"
        result.message = "path collapses to fewer than 2 unique points after duplicate removal"
        result.elapsed_seconds = time.monotonic() - start
        return result

    scale = _problem_scale(P0, raw_rects)
    result.scale = scale
    P0n = P0 / scale
    rects_n = [Rect(r.xmin / scale, r.ymin / scale, r.xmax / scale, r.ymax / scale)
               for r in raw_rects]

    # Convert physical parameters to normalized units.
    clearance_n = cfg.clearance / scale
    corridor_n = (cfg.corridor / scale) if cfg.corridor is not None else cfg.default_corridor_fraction
    min_edge_n = (cfg.min_edge_length / scale) if cfg.min_edge_length is not None else None
    kappa_cap_n = (cfg.curvature_cap * scale) if cfg.curvature_cap is not None else None

    edge_lens = np.hypot(*(P0n[1:] - P0n[:-1]).T)
    nonzero_edges = edge_lens[edge_lens > 1e-12]
    min_edge_data = float(nonzero_edges.min()) if len(nonzero_edges) else 0.0
    if min_edge_n is None:
        min_edge_n = 0.35 * min_edge_data if min_edge_data > 0 else 0.0

    # Default curvature cap: 60% of the max discrete curvature of the input.
    # Straight paths (kappa == 0 everywhere) and 2-point paths (no triples)
    # get an unbounded cap.
    k0, _ = _menger_curvature_and_jac(P0n)
    k0_max = float(k0.max()) if k0.size else 0.0
    if kappa_cap_n is None:
        kappa_cap_n = 0.6 * k0_max if k0_max > 0 else float("inf")

    result.parameters = {
        "corridor": corridor_n * scale,
        "clearance": cfg.clearance,
        "curvature_cap": (kappa_cap_n / scale) if np.isfinite(kappa_cap_n) else None,
        "min_edge_length": min_edge_n * scale,
        "deviation_weight": cfg.deviation_weight,
        "bend_weight": cfg.bend_weight,
        "jerk_weight": cfg.jerk_weight,
        "max_iterations": cfg.max_iterations,
        "timeout_seconds": cfg.timeout_seconds,
        "units": "physical",
    }

    def fail(status: str, message: str, **extra) -> SmoothResult:
        result.status = status
        result.message = message
        result.points = original          # never publish an illegal path
        result.elapsed_seconds = time.monotonic() - start
        for k, v in extra.items():
            setattr(result, k, v)
        return result

    # Dense verification spacing: target <= clearance/2, never more than
    # MAX_SUBDIVISIONS_PER_EDGE splits on a single segment.
    max_edge = float(edge_lens.max()) if len(edge_lens) else 0.0
    desired = clearance_n / 2.0 if clearance_n > 0 else max(min_edge_data, max_edge / 50.0)
    spacing_n = max(desired, max_edge / MAX_SUBDIVISIONS_PER_EDGE) if max_edge > 0 else desired

    def dense_verify(Pn: np.ndarray) -> dict:
        return _verify(Pn, rects_n, clearance_n, spacing_n, scale)

    # Gate 0: the supplied path must itself be feasible (it is the fallback).
    pre = dense_verify(P0n)
    if not pre["ok"]:
        return fail("input_collision",
                    f"original path violates clearance {cfg.clearance:g} "
                    f"(min clearance {pre['min_clearance']:.6g})",
                    verification=pre)

    if m == 2:
        # Nothing to optimize; still verified above.
        result.status = "optimal"
        result.message = "path has 2 unique points; nothing to smooth"
        result.points = original
        result.verification = pre
        result.residuals = _residuals(P0n, P0n, rects_n, corridor_n,
                                      clearance_n, kappa_cap_n, min_edge_n, scale)
        result.elapsed_seconds = time.monotonic() - start
        return result

    # --------------------------------------------------------------- setup
    nI = m - 2
    interior = np.arange(1, m - 1)
    B = np.zeros((m, nI))
    B[interior, np.arange(nI)] = 1.0
    end_fixed = np.zeros((m, 2))
    end_fixed[0] = P0n[0]
    end_fixed[-1] = P0n[-1]

    def full_points(z: np.ndarray) -> np.ndarray:
        zx, zy = z[:nI], z[nI:]
        P = np.empty((m, 2))
        P[:, 0] = B @ zx + end_fixed[:, 0]
        P[:, 1] = B @ zy + end_fixed[:, 1]
        return P

    D2 = _diff_matrix(m, 2)
    D3 = _diff_matrix(m, 3)
    D2B = D2 @ B
    D3B = D3 @ B
    wd, wb, wj = cfg.deviation_weight, cfg.bend_weight, cfg.jerk_weight

    def objective(z: np.ndarray):
        P = full_points(z)
        dev2 = float(np.sum((P - P0n) ** 2))
        bend2 = float(np.sum((D2 @ P) ** 2))
        jerk2 = float(np.sum((D3 @ P) ** 2))
        f = wd * dev2 + wb * bend2 + wj * jerk2

        gx = (2 * wd * B.T @ (P[:, 0] - P0n[:, 0])
              + 2 * wb * D2B.T @ (D2 @ P[:, 0])
              + 2 * wj * D3B.T @ (D3 @ P[:, 0]))
        gy = (2 * wd * B.T @ (P[:, 1] - P0n[:, 1])
              + 2 * wb * D2B.T @ (D2 @ P[:, 1])
              + 2 * wj * D3B.T @ (D3 @ P[:, 1]))
        return f, np.concatenate([gx, gy])

    constraints = []

    def add_pair(name, pair_fn):
        """Register a (values, jacobian) constraint with scipy.

        scipy calls the value and Jacobian callables separately; we memoize the
        last evaluation per call type and key it on the exact array identity,
        so a stale Jacobian from a previous iterate can never be used.
        """
        state = {"z": None}

        def values(z, *args):
            c, j = pair_fn(z)
            state["z"] = z
            state["c"] = c
            state["j"] = j
            return c

        def jac(z, *args):
            if state["z"] is not None and z is state["z"]:
                return state["j"]
            return pair_fn(z)[1]

        constraints.append({"type": "ineq", "fun": values, "jac": jac})

    # Corridor: disk of radius corridor_n around each original interior vertex.
    def dev_constraint(z):
        P = full_points(z)
        d = np.hypot(P[interior, 0] - P0n[interior, 0],
                     P[interior, 1] - P0n[interior, 1])
        c = corridor_n - d
        ux = np.zeros(nI)
        uy = np.zeros(nI)
        safe = d > 1e-12
        ux[safe] = -(P[interior, 0] - P0n[interior, 0])[safe] / d[safe]
        uy[safe] = -(P[interior, 1] - P0n[interior, 1])[safe] / d[safe]
        jac = np.zeros((nI, 2 * nI))
        jac[:, :nI] = np.diag(ux)
        jac[:, nI:] = np.diag(uy)
        return c, jac

    add_pair("deviation", dev_constraint)

    # Curvature cap.
    if np.isfinite(kappa_cap_n) and kappa_cap_n > 0:
        def curv_constraint(z):
            P = full_points(z)
            kappa, Jfull = _menger_curvature_and_jac(P)
            c = kappa_cap_n - kappa
            Jx = Jfull[:, 2 * interior]
            Jy = Jfull[:, 2 * interior + 1]
            jac = np.hstack([Jx, Jy])
            return c, jac

        add_pair("curvature", curv_constraint)

    # Minimum edge length.
    if min_edge_n > 0:
        def edge_constraint(z):
            P = full_points(z)
            e = P[1:] - P[:-1]
            L = np.hypot(e[:, 0], e[:, 1])
            c = L - min_edge_n
            jac = np.zeros((m - 1, 2 * nI))
            inv = np.zeros(m - 1)
            ok = L > 1e-12
            inv[ok] = 1.0 / L[ok]
            ux, uy = e[:, 0] * inv, e[:, 1] * inv
            # edge k joins vertices k (head a) and k+1 (head b)
            for k in range(m - 1):
                for vi, sx, sy in ((k, -ux[k], -uy[k]), (k + 1, ux[k], uy[k])):
                    if 1 <= vi <= m - 2:
                        col = vi - 1
                        jac[k, col] = sx
                        jac[k, nI + col] = sy
            return c, jac

        add_pair("edge_length", edge_constraint)

    # Clearance at control vertices and (optionally) control-segment midpoints.
    Cmat = np.eye(m)
    if cfg.optimize_midpoint_clearance:
        mids = np.zeros((m - 1, m))
        mids[np.arange(m - 1), np.arange(m - 1)] = 0.5
        mids[np.arange(m - 1), np.arange(1, m)] = 0.5
        Cmat = np.vstack([np.eye(m), mids])
    CI = Cmat[:, interior]

    def clearance_constraint(z):
        P = full_points(z)
        S = Cmat @ P
        if not rects_n:
            return np.ones(1), np.zeros((1, 2 * nI))
        c_parts = []
        blocks = []
        for r in rects_n:
            d = rect_signed_distance(S, r)
            c_parts.append(d - clearance_n)
            g = rect_sdf_gradients(S, r)              # (nS, 2)
            # c = sdf(S), dS/dz_interior = CI  =>  dc/dz = diag(g) @ CI
            jx = g[:, 0:1] * CI                        # (nS, nI)
            jy = g[:, 1:2] * CI                        # (nS, nI)
            blocks.append(np.hstack([jx, jy]))         # (nS, 2*nI)
        c = np.concatenate(c_parts)
        jac = np.vstack(blocks)
        return c, jac

    if rects_n:
        add_pair("clearance", clearance_constraint)

    # Bounds: square box around each original interior vertex (the disk
    # corridor is the real constraint; bounds just aid the SQP).
    lb = np.concatenate([P0n[interior, 0] - corridor_n, P0n[interior, 1] - corridor_n])
    ub = np.concatenate([P0n[interior, 0] + corridor_n, P0n[interior, 1] + corridor_n])

    z0 = np.concatenate([P0n[interior, 0], P0n[interior, 1]])
    iters = {"n": 0}
    timed_out = {"v": False}

    def callback(_xk):
        iters["n"] += 1
        if time.monotonic() - start > cfg.timeout_seconds:
            timed_out["v"] = True
            return True
        return False

    f0, _ = objective(z0)
    result.objective_components = {"objective_initial": f0}

    try:
        opt = minimize(
            objective, z0, jac=True, method="SLSQP",
            bounds=list(zip(lb, ub)),
            constraints=constraints,
            callback=callback,
            options={"maxiter": int(cfg.max_iterations), "ftol": cfg.ftol,
                     "disp": False},
        )
    except Exception as exc:  # pragma: no cover - defensive
        return fail("solver_error", f"optimizer raised: {exc!r}")

    iterations = int(max(getattr(opt, "nit", 0), iters["n"]))
    result.iterations = iterations
    result.n_variables = 2 * nI
    result.objective = float(opt.fun)

    if timed_out["v"]:
        return fail("timeout",
                    f"optimizer exceeded {cfg.timeout_seconds:g}s budget "
                    f"after {iterations} iterations",
                    iterations=iterations)

    msg = str(getattr(opt, "message", ""))
    hit_iter_budget = iterations >= cfg.max_iterations or "iteration limit" in msg.lower()

    zstar = opt.x
    Pstar = full_points(zstar)
    residuals = _residuals(Pstar, P0n, rects_n, corridor_n, clearance_n,
                           kappa_cap_n, min_edge_n, scale)
    result.residuals = residuals

    if not opt.success and hit_iter_budget:
        # SLSQP reports "iteration limit" for both slow-but-feasible progress
        # and genuine infeasibility.  If the incumbent still clearly violates
        # a hard constraint, the problem is infeasible in practice.
        max_v = residuals["max_violation_normalized"]
        if max_v < -1e-4:
            return fail("infeasible",
                        f"no feasible point within iteration budget "
                        f"({cfg.max_iterations}); max constraint violation "
                        f"{residuals['max_violation']:.3e} (physical)",
                        residuals=residuals)
        return fail("iteration_limit",
                    f"optimizer hit iteration budget ({cfg.max_iterations}) "
                    f"without converging: {msg}",
                    residuals=residuals)

    if not opt.success:
        return fail("infeasible",
                    f"optimizer did not find a feasible solution: {msg}",
                    residuals=residuals)

    # Constraint feasibility at the returned point (tight numerical gate).
    # Residual convention: positive = satisfied; reject negative violations.
    if residuals["max_violation_normalized"] < -1e-6:
        return fail("infeasible",
                    "converged point violates hard constraints "
                    f"(max violation {residuals['max_violation']:.3e} physical)",
                    residuals=residuals)

    # Gate: dense whole-trajectory collision verification.
    ver = dense_verify(Pstar)
    result.verification = ver
    if not ver["ok"]:
        return fail("postcheck_failed",
                    "solver returned a trajectory that fails dense collision "
                    f"verification (min clearance {ver['min_clearance'] * scale:.6g} < "
                    f"{cfg.clearance:g})",
                    residuals=residuals, verification=ver)

    # Success: rescale, re-expand duplicates, publish.
    Pout_norm = Pstar
    Pout = Pout_norm * scale
    expanded = expand_to_groups(Pout, groups)
    comps = _objective_components(Pstar, P0n, D2, D3)
    result.objective_components.update(comps)
    result.status = "optimal"
    result.converged = True
    result.message = f"converged after {iterations} iterations; trajectory verified collision-free"
    result.points = expanded
    result.residuals = residuals
    result.elapsed_seconds = time.monotonic() - start
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _diff_matrix(m: int, order: int) -> np.ndarray:
    """k-th order forward-difference matrix with shape (m-k, m)."""
    D = np.eye(m)
    for k in range(1, order + 1):
        n = D.shape[0]
        E = np.zeros((n - 1, n))
        E[:, :-1] -= np.eye(n - 1)
        E[:, 1:] += np.eye(n - 1)
        D = E @ D
    return D


def _objective_components(P: np.ndarray, P0: np.ndarray, D2, D3) -> dict:
    return {
        "deviation_sum_sq": float(np.sum((P - P0) ** 2)),
        "bend_sum_sq": float(np.sum((D2 @ P) ** 2)),
        "jerk_sum_sq": float(np.sum((D3 @ P) ** 2)),
        "max_deviation": float(np.max(np.hypot(P[:, 0] - P0[:, 0],
                                               P[:, 1] - P0[:, 1]))),
    }


def _residuals(P: np.ndarray, P0: np.ndarray, rects: list[Rect],
               corridor_n: float, clearance_n: float,
               kappa_cap_n: float, min_edge_n: float, scale: float) -> dict:
    """Constraint residual block.

    Each residual is (limit - observed) in physical units; positive means
    satisfied.  Normalized variants are included for diagnostics at scale.
    """
    interior = np.arange(1, len(P) - 1)
    dev = np.hypot(P[interior, 0] - P0[interior, 0],
                   P[interior, 1] - P0[interior, 1]) if len(P) > 2 else np.zeros(0)
    kappa, _ = _menger_curvature_and_jac(P)
    kappa_max = float(np.max(kappa)) if kappa.size else 0.0
    edges = np.hypot(*(P[1:] - P[:-1]).T) if len(P) > 1 else np.zeros(0)
    endpoint_err = max(
        float(np.hypot(*(P[0] - P0[0]))),
        float(np.hypot(*(P[-1] - P0[-1]))),
    )

    min_sdf = float("inf")
    samples = []
    samples.append(P)
    if len(P) > 1:
        samples.append((P[1:] + P[:-1]) / 2.0)
    S = np.vstack(samples)
    for r in rects:
        min_sdf = min(min_sdf, float(np.min(rect_signed_distance(S, r))))

    violations_n = {
        "deviation": float(corridor_n - np.max(dev)) if len(dev) else float("inf"),
        "curvature": float(kappa_cap_n - kappa_max) if np.isfinite(kappa_cap_n) else float("inf"),
        "min_edge_length": float(np.min(edges) - min_edge_n) if len(edges) else float("inf"),
        "clearance": float(min_sdf - clearance_n) if rects else float("inf"),
        "fixed_endpoints": -endpoint_err,
    }
    physical = {
        "deviation": violations_n["deviation"] * scale,
        "curvature": violations_n["curvature"] / scale,
        "min_edge_length": violations_n["min_edge_length"] * scale,
        "clearance": violations_n["clearance"] * scale,
        "fixed_endpoints": violations_n["fixed_endpoints"] * scale,
    }
    observed_n = {
        "max_deviation": float(np.max(dev)) if len(dev) else 0.0,
        "max_curvature": kappa_max,
        "min_edge_length": float(np.min(edges)) if len(edges) else 0.0,
        "min_clearance": min_sdf if rects else float("inf"),
        "endpoint_error": endpoint_err,
    }
    observed_phys = {
        "max_deviation": observed_n["max_deviation"] * scale,
        "max_curvature": observed_n["max_curvature"] / scale,
        "min_edge_length": observed_n["min_edge_length"] * scale,
        "min_clearance": (observed_n["min_clearance"] * scale) if rects else None,
        "endpoint_error": observed_n["endpoint_error"] * scale,
    }
    finite_vals = [v for v in violations_n.values() if np.isfinite(v)]
    return {
        "max_violation": min(finite_vals) * scale if finite_vals else 0.0,
        "max_violation_normalized": min(finite_vals) if finite_vals else 0.0,
        "residuals_physical": physical,
        "residuals_normalized": violations_n,
        "observed_physical": observed_phys,
        "observed_normalized": observed_n,
        "satisfied": all(v >= -1e-7 for v in finite_vals),
    }


def _verify(Pn: np.ndarray, rects_n: list[Rect], clearance_n: float,
            spacing_n: float, scale: float) -> dict:
    dense = densify(Pn, spacing_n)
    tol = 1e-9
    min_sdf = float("inf")
    worst = None
    for r in rects_n:
        d = rect_signed_distance(dense, r)
        idx = int(np.argmin(d))
        if float(d[idx]) < min_sdf:
            min_sdf = float(d[idx])
            worst = dense[idx]

    min_seg = float("inf")
    worst_seg = None
    for i, (a, b) in enumerate(zip(Pn[:-1], Pn[1:])):
        for r in rects_n:
            d = segment_rect_distance(a, b, r)
            if d < min_seg:
                min_seg, worst_seg = float(d), i

    overall = min(min_sdf, min_seg) if rects_n else float("inf")
    ok = overall + tol >= clearance_n
    return {
        "ok": bool(ok),
        "min_clearance": overall * scale if rects_n else None,
        "min_sample_sdf": min_sdf * scale if rects_n else None,
        "min_segment_distance": min_seg * scale if rects_n else None,
        "samples_checked": int(len(dense)),
        "control_segments_checked": int(len(Pn) - 1),
        "sample_spacing": spacing_n * scale,
        "worst_sample": None if worst is None else [float(worst[0] * scale),
                                                    float(worst[1] * scale)],
        "worst_segment_index": worst_seg,
        "required_clearance": clearance_n * scale,
    }
