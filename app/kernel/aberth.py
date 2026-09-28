"""Aberth-Ehrlich simultaneous iteration.

Contract on iteration exhaustion: this kernel NEVER raises for
non-convergence. When the iteration budget runs out it returns the last
iterate with per-root ``converged=False`` flags, so the service layer can
report a PARTIAL result with the unconverged state preserved instead of
silently dropping or mislabelling roots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from ..domain import NormalizedPolynomial
from ..errors import ComputationFailedError
from .polyeval import poly_derivative_coeffs, poly_eval


@dataclass
class AberthResult:
    roots: np.ndarray                 # final iterate (converged or not)
    converged: np.ndarray             # per-root bool
    iterations: np.ndarray            # per-root iteration count at convergence/stop
    steps_taken: int                  # total sweeps actually performed
    max_step_history: List[float] = field(default_factory=list)  # max |w| per sweep


def cauchy_initial_guesses(poly: NormalizedPolynomial) -> np.ndarray:
    """Roots of unity scaled by the Cauchy bound, with a deterministic
    irrational-angle twist so symmetric polynomials do not lock the iteration."""
    c = poly.coeffs / poly.coeffs[0]
    radius = 1.0 + float(np.max(np.abs(c[1:]))) if poly.degree > 0 else 1.0
    n = poly.degree
    k = np.arange(n)
    angles = 2.0 * np.pi * k / n + 0.717356  # deterministic, non-symmetric offset
    return radius * np.exp(1j * angles)


def aberth_refine(
    poly: NormalizedPolynomial,
    initial: np.ndarray,
    *,
    max_iter: int,
    conv_tol: float,
    run_id: Optional[str] = None,
) -> AberthResult:
    """Refine all roots simultaneously.

    A root is converged when its Aberth correction satisfies
    |w| <= conv_tol * max(1, |z|). Roots keep their last state on exhaustion.
    """
    if max_iter < 0:
        raise ComputationFailedError("max_iter must be >= 0", run_id=run_id)

    coeffs = poly.coeffs
    dcoeffs = poly_derivative_coeffs(coeffs)
    n = poly.degree
    z = np.array(initial, dtype=np.complex128, copy=True)
    if z.shape[0] != n:
        raise ComputationFailedError(
            "initial guess count does not match polynomial degree",
            detail={"guesses": int(z.shape[0]), "degree": n},
            run_id=run_id,
        )

    converged = np.zeros(n, dtype=bool)
    iterations = np.zeros(n, dtype=int)
    history: List[float] = []
    steps_taken = 0

    for sweep in range(max_iter):
        active = np.flatnonzero(~converged)
        if active.size == 0:
            break
        steps_taken = sweep + 1

        # The offset sum always spans ALL roots (converged ones included);
        # only the update is restricted to unconverged roots. Dropping
        # converged roots from the sum would corrupt the deflation and can
        # throw the remaining iterates far away.
        diff = z[:, None] - z[None, :]
        coincident = np.abs(diff) == 0.0
        np.fill_diagonal(coincident, False)
        if np.any(coincident):
            nudge = (1e-8 + 1e-8j) * (1.0 + np.abs(z))
            z = z + nudge * np.linspace(1.0, 2.0, n)
            diff = z[:, None] - z[None, :]
        np.fill_diagonal(diff, 1.0)  # placeholder; excluded below
        inv = 1.0 / diff
        np.fill_diagonal(inv, 0.0)   # j == i contributes nothing to the sum
        inv_sum = np.sum(inv, axis=1)

        p = poly_eval(coeffs, z[active])
        dp = poly_eval(dcoeffs, z[active])
        safe_dp = np.where(dp == 0.0, complex(np.inf), dp)
        newton = p / safe_dp
        denom = 1.0 - newton * inv_sum[active]
        safe_denom = np.where(denom == 0.0, complex(np.inf), denom)
        w = newton / safe_denom

        if not np.all(np.isfinite(w)):
            raise ComputationFailedError(
                "Aberth iteration produced a non-finite correction "
                "(vanishing derivative or degenerate root configuration)",
                detail={"sweep": steps_taken},
                run_id=run_id,
            )

        z[active] -= w
        iterations[active] += 1
        step = np.abs(w)
        history.append(float(np.max(step)))
        newly = step <= conv_tol * np.maximum(1.0, np.abs(z[active]))
        converged[active[newly]] = True

    if not np.all(np.isfinite(z)):
        raise ComputationFailedError(
            "Aberth iteration produced non-finite roots",
            detail={"sweeps": steps_taken},
            run_id=run_id,
        )

    return AberthResult(
        roots=z,
        converged=converged,
        iterations=iterations,
        steps_taken=steps_taken,
        max_step_history=history,
    )
