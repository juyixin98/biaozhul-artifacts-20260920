"""Weighted covariate-balance diagnostic for propensity-model misspecification.

IPW is robust to a misspecified *outcome* model but not to a misspecified
*propensity* model. Calibration of fitted scores against treatment can miss a
misspecification that is orthogonal to the fitted linear index (e.g. an
omitted quadratic). Weighted covariate balance catches it directly: if the
estimated weights are correct, re-weighting must balance *every* pre-treatment
covariate function in the target population -- including low-order
non-linearities the linear propensity index could miss.

We test an augmented basis (raw covariates, their squares, and pairwise
interactions) and report the largest standardized mean difference expressed
as a z-statistic of the two weighted means (Hájek sandwich variances). Under
a correctly specified, adequate model these are approximately standard
normal; a large value is evidence the propensity model is inadequate.
"""

from __future__ import annotations

import numpy as np

BALANCE_BASIS = "linear_plus_squares_and_pairwise_interactions"


def augmented_basis(x: np.ndarray) -> np.ndarray:
    """Return [X, X**2, X_i*X_j for i<j]."""
    p = x.shape[1]
    cols: list[np.ndarray] = [x, x**2]
    for a in range(p):
        for b in range(a + 1, p):
            cols.append((x[:, a] * x[:, b])[:, None])
    return np.hstack(cols)


def _weighted_mean_var(
    values: np.ndarray, weights: np.ndarray
) -> tuple[float, float]:
    """Hájek weighted mean and its sandwich variance (sum w)^-2 sum w^2 r^2."""
    total = float(weights.sum())
    if total <= 0.0:
        return float("nan"), float("inf")
    mean = float(np.sum(weights * values) / total)
    resid = weights * (values - mean)
    var = float(np.sum(resid**2)) / (total**2)
    return mean, var


def weighted_balance_z(
    treatment: np.ndarray, weights: np.ndarray, basis: np.ndarray
) -> np.ndarray:
    """Absolute z-statistic of the treated-minus-control weighted mean per
    basis column."""
    treated = treatment == 1
    control = ~treated
    z_stats = np.zeros(basis.shape[1])
    for j in range(basis.shape[1]):
        m1, v1 = _weighted_mean_var(basis[treated, j], weights[treated])
        m0, v0 = _weighted_mean_var(basis[control, j], weights[control])
        se = float(np.sqrt(v1 + v0))
        z_stats[j] = abs(m1 - m0) / se if se > 1e-12 and np.isfinite(se) else 0.0
    return z_stats


def max_balance_z(
    treatment: np.ndarray, weights: np.ndarray, covariates: np.ndarray
) -> float:
    """Largest absolute balance z over the augmented basis."""
    basis = augmented_basis(covariates)
    return float(np.max(weighted_balance_z(treatment, weights, basis)))
