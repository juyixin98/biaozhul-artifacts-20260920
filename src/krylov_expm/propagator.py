"""Time segmentation and restart orchestration (fixed rules, see SolverConfig)."""
from __future__ import annotations

import math

import numpy as np
from scipy.sparse.linalg import norm as sparse_norm

from .config import SolverConfig
from .errors import ExpmvFailure, FailureCategory
from .evidence import ErrorEvidence, SegmentEvidence
from .kernel import krylov_step


def plan_segment_count(t: float, norm_a: float, config: SolverConfig) -> int:
    """Fixed segmentation rule: ceil(|t| * ||A||_1 / theta), clamped."""
    if t == 0.0 or norm_a == 0.0:
        return 1
    needed = math.ceil(abs(t) * norm_a / config.segment_theta)
    return max(1, min(needed, config.max_segments))


def _advance(matrix, w, dt, tol, config, depth):
    """Propagate one (sub-)step; halve and retry on non-convergence."""
    res = krylov_step(matrix, w, dt, config)
    scale = max(1.0, float(np.linalg.norm(res.vector)))
    if res.error_estimate <= tol * scale:
        evidence = SegmentEvidence(
            segment_index=-1,
            step_size=dt,
            krylov_dim=res.krylov_dim,
            subspace_residual_norm=res.subspace_residual_norm,
            error_estimate=res.error_estimate,
            restart_splits=depth,
            matvec_count=res.matvec_count,
            converged=True,
        )
        return res.vector, evidence
    if depth >= config.max_restart_splits:
        raise ExpmvFailure(
            FailureCategory.NOT_CONVERGED,
            f"error estimate {res.error_estimate:.3e} exceeds tolerance {tol:.1e} "
            f"after {depth} restart splits (krylov_dim={res.krylov_dim})",
            details={
                "error_estimate": res.error_estimate,
                "tol": tol,
                "krylov_dim": res.krylov_dim,
                "restart_splits": depth,
                "step_size": dt,
            },
        )
    w1, ev1 = _advance(matrix, w, dt / 2.0, tol, config, depth + 1)
    w2, ev2 = _advance(matrix, w1, dt / 2.0, tol, config, depth + 1)
    merged = SegmentEvidence(
        segment_index=-1,
        step_size=dt,
        krylov_dim=max(ev1.krylov_dim, ev2.krylov_dim),
        subspace_residual_norm=ev1.subspace_residual_norm + ev2.subspace_residual_norm,
        error_estimate=ev1.error_estimate + ev2.error_estimate,
        restart_splits=max(ev1.restart_splits, ev2.restart_splits),
        matvec_count=ev1.matvec_count + ev2.matvec_count,
        converged=ev1.converged and ev2.converged,
    )
    return w2, merged


def propagate(matrix, v, t, tol, config: SolverConfig):
    """Compute exp(tA)v by segmented Krylov propagation.

    Returns (vector, ErrorEvidence). Raises ExpmvFailure(NOT_CONVERGED)
    when the fixed restart rule cannot meet the tolerance.
    """
    norm_a = float(sparse_norm(matrix, 1)) if matrix.nnz else 0.0
    n_seg = plan_segment_count(t, norm_a, config)
    dt = t / n_seg
    evidence = ErrorEvidence(norm_a_1=norm_a, planned_segments=n_seg)
    w = np.array(v, dtype=np.float64, copy=True)
    for idx in range(n_seg):
        w, segment = _advance(matrix, w, dt, tol, config, depth=0)
        segment.segment_index = idx
        evidence.add_segment(segment)
    return w, evidence
