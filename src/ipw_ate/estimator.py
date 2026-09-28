"""ATE/ATT point estimator and influence-function standard error.

Point estimate (stable/Hájek):

    mu1 = mean over treated of w1*Y,  mu0 = mean over control of w0*Y
    tau = mu1 - mu0

where the within-arm weights come from :mod:`ipw_ate.weights`.

Uncertainty uses the out-of-fold (cross-fitted) scores and the *realized
trimmed* weights in an influence-function-style sum. Per unit::

    ATE:  g_i = t * w1 (Y-mu1)/n1  - (1-t) * w0 (Y-mu0)/n0
    ATT:  g_i = t *    (Y-mu1)/n1  - (1-t) * w0 (Y-mu0)/n0

``SE(tau) = sqrt(sum_i g_i^2)``. Scores are out-of-fold, so this does not
reuse in-sample fits. The normal approximation gives the CI. It is a
frequentist interval *under the declared assumptions*, not a causal proof.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.stats import norm

from .contract import Estimand


def weighted_arm_means(
    treatment: np.ndarray, outcome: np.ndarray, weights: np.ndarray
) -> tuple[float, float, np.ndarray, np.ndarray]:
    treated = treatment == 1
    control = ~treated
    n1 = int(np.sum(treated))
    n0 = int(np.sum(control))
    if n1 == 0 or n0 == 0:
        raise ZeroDivisionError("both arms required for arm means")
    mu1 = float(np.sum(weights[treated] * outcome[treated]) / n1)
    mu0 = float(np.sum(weights[control] * outcome[control]) / n0)
    return mu1, mu0, treated, control


def estimate_ate(
    treatment: np.ndarray,
    outcome: np.ndarray,
    weights: np.ndarray,
    estimand: Estimand,
    ci_level: float = 0.95,
) -> tuple[float, float, float, float]:
    """Return ``(tau, se, ci_lower, ci_upper)``."""
    mu1, mu0, treated, control = weighted_arm_means(treatment, outcome, weights)
    n1, n0 = int(np.sum(treated)), int(np.sum(control))
    tau = mu1 - mu0

    resid1 = outcome[treated] - mu1
    resid0 = outcome[control] - mu0

    g = np.zeros_like(outcome)
    if estimand is Estimand.ATE:
        g[treated] = weights[treated] * resid1 / n1
        g[control] = -weights[control] * resid0 / n0
    else:  # ATT: treated arm carries unit weight
        g[treated] = resid1 / n1
        g[control] = -weights[control] * resid0 / n0

    se = float(math.sqrt(float(np.sum(g * g))))
    if not math.isfinite(se):
        raise FloatingPointError("non-finite standard error")

    z = float(norm.ppf(0.5 + ci_level / 2.0))
    return tau, se, tau - z * se, tau + z * se
