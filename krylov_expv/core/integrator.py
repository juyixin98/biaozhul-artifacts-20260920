"""Time-stepping Krylov integrator for w = exp(t*A) @ v.

Fixed segmentation and restart rules (no hidden adaptivity):

* one Arnoldi basis per step, dimension at most ``m_max`` -- every accepted
  step *is* a restart (the basis is rebuilt from the new state);
* a candidate step is halved (factor ``step_shrink``) until its error
  estimate meets the local tolerance share ``tol * ||w|| * |tau| / |t|``,
  at most ``max_halvings`` times;
* the next trial step doubles (factor ``step_grow``) only when the estimate
  landed below ``grow_threshold`` of the local tolerance;
* ``max_steps`` accepted steps cap the whole integration; exceeding the cap
  returns a *not converged* result with the best state so far.

Degenerate inputs are handled explicitly: t == 0 returns a copy of v, the
zero vector returns zeros, and negative t integrates backward through the
same code path (tau carries the sign).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp

from ..config import ExpvConfig
from ..errors import MemoryBudgetExceeded, NotConverged, StepSizeUnderflow
from ..evidence import ExpvEvidence, StepEvidence
from .arnoldi import arnoldi
from .dense_expm import augmented_expm_action
from .estimator import step_error_quantities


@dataclass(frozen=True)
class ExpvResult:
    """Outcome of one exp(t*A) @ v evaluation."""

    w: np.ndarray
    converged: bool
    evidence: ExpvEvidence


def _check_memory_budget(n: int, config: ExpvConfig) -> int:
    needed = config.basis_memory_bytes(n)
    if needed > config.memory_budget_bytes:
        raise MemoryBudgetExceeded(
            f"basis storage for n={n}, m_max={config.m_max} needs {needed} bytes, "
            f"budget is {config.memory_budget_bytes} bytes"
        )
    return needed


def _trivial_result(w: np.ndarray, termination: str, evidence: ExpvEvidence) -> ExpvResult:
    evidence.termination = termination
    evidence.finish()
    return ExpvResult(w=w, converged=True, evidence=evidence)


def expv(
    matrix: sp.spmatrix,
    t: float,
    v: np.ndarray,
    config: ExpvConfig | None = None,
    tol: float | None = None,
) -> ExpvResult:
    """Compute w ~= exp(t*A) @ v with full error evidence.

    Raises:
        MemoryBudgetExceeded: before doing any work, if the basis storage
            would exceed the configured budget.
    """
    config = config or ExpvConfig()
    tolerance = config.tol if tol is None else tol
    evidence = ExpvEvidence()

    n = matrix.shape[0]
    if matrix.shape[0] != matrix.shape[1]:
        raise ValueError(f"matrix must be square, got shape {matrix.shape}")
    if v.shape != (n,):
        raise ValueError(f"vector shape {v.shape} does not match matrix dimension {n}")

    # --- explicit degenerate cases -------------------------------------
    if t == 0.0:
        return _trivial_result(v.astype(np.float64).copy(), "t_zero", evidence)
    beta0 = float(np.linalg.norm(v))
    if beta0 == 0.0:
        return _trivial_result(np.zeros(n, dtype=np.float64), "zero_vector", evidence)

    evidence.memory_bytes_used = _check_memory_budget(n, config)

    def apply(x: np.ndarray) -> np.ndarray:
        return matrix @ x

    w = v.astype(np.float64).copy()
    t_total = abs(t)
    remaining = float(t)
    tau = remaining
    step_index = 0

    while remaining != 0.0:
        if step_index >= config.max_steps:
            evidence.record_failure(
                NotConverged.category,
                f"tolerance {tolerance:.3e} not reached after {step_index} steps; "
                f"|remaining t| = {abs(remaining):.6e}, "
                f"accumulated error estimate = {evidence.total_error_estimate:.3e}",
            )
            return ExpvResult(w=w, converged=False, evidence=evidence)

        tau = math.copysign(min(abs(tau), abs(remaining)), remaining)
        arn = arnoldi(apply, w, config.m_max, config.breakdown_tol)
        H_square = arn.hessenberg[: arn.dim, : arn.dim]
        h_next = float(arn.hessenberg[arn.dim, arn.dim - 1])
        scale = max(float(np.linalg.norm(w)), 1.0e-300)

        accepted = None
        halvings = 0
        while True:
            dense = augmented_expm_action(H_square, tau)
            quantities = step_error_quantities(arn.beta, h_next, dense)
            tol_local = tolerance * scale * abs(tau) / t_total
            if quantities.error_estimate <= config.acceptance_safety * tol_local or arn.happy_breakdown:
                accepted = (dense, quantities, tol_local)
                break
            if halvings >= config.max_halvings:
                evidence.record_failure(
                    StepSizeUnderflow.category,
                    f"step {step_index}: {halvings} halvings at t={t - remaining:.6e} "
                    f"still above tolerance (estimate {quantities.error_estimate:.3e} "
                    f"> local tol {tol_local:.3e})",
                )
                return ExpvResult(w=w, converged=False, evidence=evidence)
            tau *= config.step_shrink
            halvings += 1

        dense, quantities, tol_local = accepted
        t_before = t - remaining
        w = arn.beta * (arn.basis[:, : arn.dim] @ dense.step)
        remaining -= tau
        if abs(remaining) <= 1.0e-15 * t_total:
            remaining = 0.0

        evidence.steps.append(
            StepEvidence(
                index=step_index,
                t_before=t_before,
                tau=tau,
                krylov_dim=arn.dim,
                subspace_residual=quantities.subspace_residual,
                error_estimate=quantities.error_estimate,
                accepted=True,
                halvings=halvings,
                happy_breakdown=arn.happy_breakdown,
            )
        )
        evidence.total_error_estimate += quantities.error_estimate
        evidence.max_subspace_residual = max(
            evidence.max_subspace_residual, quantities.subspace_residual
        )

        # deterministic next-step size
        if quantities.error_estimate < config.grow_threshold * tol_local:
            tau = tau * config.step_grow
        step_index += 1

    evidence.finish()
    return ExpvResult(w=w, converged=True, evidence=evidence)
