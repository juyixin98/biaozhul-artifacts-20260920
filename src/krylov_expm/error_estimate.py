"""A posteriori error evidence for one Krylov step.

Two distinct quantities are reported and must not be conflated:

* subspace residual norm -- the *exact* norm of the ODE residual of the
  Krylov approximation w(t) = beta * V_k exp(t H_k) e_1 at the end of the
  step. Since the residual is rank one,
      r(t) = A w(t) - w'(t)
           = -beta * h_{k+1,k} * (e_k^T exp(t H_k) e_1) * v_{k+1},
  its norm is exactly beta * h_{k+1,k} * |e_k^T exp(t H_k) e_1|.

* error estimate -- a bound-like estimate for ||w(t) - exp(tA)v||. The
  error e satisfies e' = A e + r with e(0) = 0, hence
  ||e(t)|| <= integral_0^t ||r(tau)|| d tau. The integrand is evaluated
  on the small reduced Hessenberg system by quadrature, which makes this
  estimate cheap and independent of the endpoint residual alone.
"""
from __future__ import annotations

import numpy as np
import scipy.linalg


def endpoint_residual_norm(beta: float, h_next: float, last_entry: float) -> float:
    """Exact norm of the rank-one ODE residual at the step endpoint."""
    return float(abs(beta * h_next * last_entry))


def integrated_error_estimate(
    beta: float,
    h_next: float,
    hessenberg: np.ndarray,
    t: float,
    num_points: int,
) -> float:
    """Trapezoidal estimate of integral_0^t ||r(tau)|| d tau on the reduced system."""
    k = hessenberg.shape[0]
    taus = np.linspace(0.0, t, num_points + 1)
    g = np.empty(num_points + 1)
    for i, tau in enumerate(taus):
        if tau == 0.0:
            g[i] = 1.0 if k == 1 else 0.0
        else:
            exp_th = scipy.linalg.expm(tau * hessenberg)
            g[i] = abs(exp_th[-1, 0])
    integral = float(np.trapezoid(g, x=taus))
    return float(abs(beta * h_next * integral))
