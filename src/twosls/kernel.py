"""2SLS estimation kernel.

This module contains *only* the math. Inputs are validated ``EstimationData``;
outputs are raw numeric artifacts. Diagnostics live in ``diagnostics.py`` and
orchestration / status decisions in ``estimator.py``.

Model
-----
    y = Y beta + X gamma + e        (structural)
    Y = X Pi_x + Z Pi_z + V         (first stage)

2SLS:
    1. Y_hat = P_Q Y,  Q = [X, Z]
    2. delta = [beta; gamma] solves the IV normal equations
           W' P_Q W delta = W' P_Q y,   W = [Y, X]

Crucial standard-error point
----------------------------
The second-stage regression of y on [Y_hat, X] would report a residual
variance computed from y - [Y_hat, X] delta, which is wrong: Y_hat != Y.
The 2SLS asymptotic variance uses the *structural* residuals
    e_hat = y - Y beta - X gamma
with the P_Q projection matrix:

    homoskedastic : sigma2_e * (W' P_Q W)^-1
    robust (HC1)  : B * [W' P_Q diag(e^2) P_Q W] * B,
                    B = (W' P_Q W)^-1

All n x n projections are evaluated through cross-products
    A' P_Q B = (Q'A)' (Q'Q)^-1 (Q'B)
so memory stays O((k+m+L)^2 n).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .linalg import pinv_sym, solve_spd


@dataclass(frozen=True)
class KernelArtifacts:
    # structural
    delta: np.ndarray                 # (k+m,) [beta_endog, gamma_exog]
    residuals: np.ndarray             # (n,) structural residuals e_hat
    sigma2: float                     # e'e / (n - k - m)
    vcov: np.ndarray                  # (r,r) estimated covariance of delta
    y_hat_structural: np.ndarray      # W @ delta
    # first stage
    first_stage_coefs: np.ndarray     # (m+L, k) coefficients of Q=[X,Z] on Y
    Y_hat: np.ndarray                 # (n,k) P_Q Y
    first_stage_resid: np.ndarray     # (n,k) V_hat = Y - Y_hat
    # cross-product bread, reused by diagnostics
    WpQW: np.ndarray                  # (r,r)
    Q: np.ndarray                     # (n, m+L)
    W: np.ndarray                     # (n, k+m)
    PqW: np.ndarray                   # (n, r) P_Q W (=[Y_hat, X])


def _cross_through_projection(
    Q: np.ndarray, QtQ_inv: np.ndarray, A: np.ndarray, B: np.ndarray
) -> np.ndarray:
    """A' P_Q B without forming P_Q."""
    QtA = Q.T @ A
    QtB = Q.T @ B
    return QtA.T @ QtQ_inv @ QtB


def _robust_vcov(
    W: np.ndarray, PqW: np.ndarray, WpQW_inv: np.ndarray, resid: np.ndarray, dof: int
) -> np.ndarray:
    """HC1 heteroskedasticity-robust covariance for 2SLS."""
    r_e = PqW * resid[:, None]            # P_Q diag(e^2) P_Q W  (right half)
    meat = r_e.T @ r_e                    # W' P_Q diag(e^2) P_Q W
    n = W.shape[0]
    hc1 = n / max(n - dof, 1)
    return hc1 * (WpQW_inv @ meat @ WpQW_inv)


def run_kernel(data, covariance: str = "homoskedastic", request_id: str = "") -> KernelArtifacts:
    """Compute the 2SLS estimate and its covariance.

    ``data`` is an ``EstimationData``; the type is left un-annotated to keep
    this module importable without pydantic for experiment scripts.
    """
    y, Y, X, Z = data.y, data.Y, data.X, data.Z
    n = y.shape[0]
    k, m, L = Y.shape[1], X.shape[1], Z.shape[1]
    r = k + m

    Q = np.column_stack([X, Z]) if Z.shape[1] else X
    W = np.column_stack([Y, X])

    QpQ_inv = pinv_sym(Q.T @ Q)
    WpQW = _cross_through_projection(Q, QpQ_inv, W, W)
    WpQy = _cross_through_projection(Q, QpQ_inv, W, y[:, None]).ravel()

    # IV normal equations; SPD bread required for a finite variance estimate
    delta = solve_spd(WpQW, WpQy, request_id or data.request_id, what="W'P_QW")
    resid = y - W @ delta
    dof = n - r
    if dof <= 0:
        # Defensive: data.py checks this first, but the kernel must not divide.
        raise ValueError("non-positive residual degrees of freedom")
    sigma2 = float(resid @ resid / dof)

    # P_Q W = Q (Q'Q)^-1 Q'W  (n x r), evaluated without forming P_Q
    PqW = Q @ (QpQ_inv @ (Q.T @ W))

    WpQW_inv = pinv_sym(WpQW)
    if covariance == "homoskedastic":
        vcov = sigma2 * WpQW_inv
    elif covariance == "robust":
        vcov = _robust_vcov(W, PqW, WpQW_inv, resid, dof=r)
    else:  # pragma: no cover - guarded by pydantic Literal
        raise ValueError(f"unknown covariance kind: {covariance}")

    # First stage: Y on Q
    fs_coefs = QpQ_inv @ (Q.T @ Y)
    Y_hat = Q @ fs_coefs
    fs_resid = Y - Y_hat

    return KernelArtifacts(
        delta=delta,
        residuals=resid,
        sigma2=sigma2,
        vcov=vcov,
        y_hat_structural=W @ delta,
        first_stage_coefs=fs_coefs,
        Y_hat=Y_hat,
        first_stage_resid=fs_resid,
        WpQW=WpQW,
        Q=Q,
        W=W,
        PqW=PqW,
    )
