"""Dense exponentials of the *small* projected matrices only.

The full sparse A is never exponentiated.  Everything here operates on the
(k+1)x(k+1) augmented Hessenberg matrix, k <= m_max, via scipy's scaling
and squaring.  One augmented exponential yields both quantities the
integrator needs:

    exp(tau * [[H, e1], [0, 0]])  =  [[exp(tau*H), tau*phi_1(tau*H) e1],
                                      [0,          1                  ]]

so the step vector and the phi-based error estimate come from a single
dense factorisation.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.linalg


@dataclass(frozen=True)
class AugmentedExponential:
    """Dense exponential of the augmented projected system.

    Attributes:
        step: exp(tau*H) @ e1 -- coefficients of the Krylov approximation.
        phi_last: last component of tau*phi_1(tau*H) @ e1, the quantity the
            error estimate is built from.
    """

    step: np.ndarray
    phi_last: float


def augmented_expm_action(hessenberg_square: np.ndarray, tau: float) -> AugmentedExponential:
    """Evaluate exp and phi_1 of tau*H applied to e1 via one augmented expm."""
    k = hessenberg_square.shape[0]
    aug = np.zeros((k + 1, k + 1), dtype=np.float64)
    aug[:k, :k] = tau * hessenberg_square
    aug[0, k] = 1.0  # e1 in the augmented corner
    E = scipy.linalg.expm(aug)
    return AugmentedExponential(step=E[:k, 0].copy(), phi_last=float(E[k - 1, k]))
