"""A-posteriori error quantities for one Krylov step.

Two *distinct* quantities are reported and must never be conflated:

* subspace residual norm -- the ODE residual of the Krylov approximation
  ``w(tau) = beta * V exp(tau H) e1`` satisfies
  ``A w - w' = beta * h_{k+1,k} * (e_k^T exp(tau H) e1) * v_{k+1}``,
  so its norm is ``beta * h_{k+1,k} * |step[-1]|``.  It measures how well
  the approximation satisfies the differential equation.

* error estimate -- the leading neglected term of the Krylov expansion,
  ``beta * h_{k+1,k} * |tau * phi_1(tau H)_{k,1}|``, the standard
  Expokit-style estimate of the actual approximation error.

Both are derived from the same augmented dense exponential but they are
different numbers; the evidence record keeps them side by side.
"""

from __future__ import annotations

from dataclasses import dataclass

from .dense_expm import AugmentedExponential


@dataclass(frozen=True)
class StepErrorQuantities:
    subspace_residual: float
    error_estimate: float


def step_error_quantities(
    beta: float, h_next: float, dense: AugmentedExponential
) -> StepErrorQuantities:
    """Combine Arnoldi scalars with the dense exponential into error numbers."""
    residual = beta * h_next * abs(dense.step[-1])
    estimate = beta * h_next * abs(dense.phi_last)
    return StepErrorQuantities(subspace_residual=residual, error_estimate=estimate)
