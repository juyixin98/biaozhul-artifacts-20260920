"""Local synthetic data generators with a *known* generative process.

These exist so results are independently verifiable: the true ATE and the
true propensity are known by construction, so test fixtures do not depend on
the estimator under test to produce reference answers.

Scenarios
~~~~~~~~~
- :func:`make_overlap_data`        good overlap, constant additive ATE
- :func:`make_no_overlap_data`     a covariate region is single-arm only
- :func:`make_misspecified_data`   correct treatment but outcome model is
                                   non-linear so a linear mean model would
                                   mismatch (propensity still logistic)
- :func:`make_extreme_weights_data` scores pushed near 0/1 (interior)
- :func:`make_separated_data`      deterministic treatment given X (scores
                                   converge to 0/1 -> undefined weights)
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

TRUE_ATE: float = 2.0
INTERCEPT: float = -0.3


@dataclass(frozen=True)
class SyntheticData:
    treatment: np.ndarray
    outcome: np.ndarray
    covariates: np.ndarray
    true_propensity: np.ndarray
    true_ate: float
    feature_names: tuple[str, ...]


def _outcome(t: np.ndarray, x: np.ndarray) -> np.ndarray:
    # Y(0) = linear in X + noise; constant additive effect TRUE_ATE.
    return INTERCEPT + 1.5 * x[:, 0] - 1.0 * x[:, 1] + TRUE_ATE * t


def _sample(
    n: int,
    *,
    seed: int,
    logit_shift: float = 0.0,
    logit_scale: float = 1.0,
    force_separation: bool = False,
    nonlinear_outcome: bool = False,
) -> SyntheticData:
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, 2))
    if force_separation:
        # Deterministic assignment from the sign of a linear index.
        eta = 1.0 * x[:, 0] - 0.5 * x[:, 1]
        t = (eta > 0.0).astype(int)
        # True propensity is a step function (0/1); keep finite placeholder.
        true_ps = np.where(eta > 0.0, 1.0, 0.0).astype(float)
    else:
        eta = logit_shift + logit_scale * (0.8 * x[:, 0] - 0.6 * x[:, 1])
        p = 1.0 / (1.0 + np.exp(-eta))
        t = (rng.uniform(size=n) < p).astype(int)
        true_ps = p

    base = INTERCEPT + 1.5 * x[:, 0] - 1.0 * x[:, 1]
    if nonlinear_outcome:
        base = base + 2.0 * x[:, 0] ** 2 - 1.5 * np.sin(x[:, 1])
    y = base + TRUE_ATE * t + rng.normal(scale=0.5, size=n)
    return SyntheticData(
        treatment=t,
        outcome=y,
        covariates=x,
        true_propensity=true_ps,
        true_ate=TRUE_ATE,
        feature_names=("x0", "x1"),
    )


def make_overlap_data(n: int = 600, seed: int = 11) -> SyntheticData:
    """Good-overlap logistic assignment; ATE = TRUE_ATE by construction."""
    return _sample(n, seed=seed)


def make_extreme_weights_data(n: int = 600, seed: int = 22) -> SyntheticData:
    """Steep logit -> many interior-but-extreme scores, weak overlap.

    The slope is chosen so scores stay strictly inside (0,1) (weights remain
    defined) but a substantial fraction fall beyond 0.01/0.99, which must
    surface as an INCONCLUSIVE weak-overlap verdict rather than silently pass.
    """
    return _sample(n, seed=seed, logit_scale=2.5)


def make_misspecified_data(n: int = 600, seed: int = 33) -> SyntheticData:
    """Non-linear *outcome* surface with a correctly-specified (logistic)
    propensity.

    IPW does not model the outcome, so this must still recover the additive
    effect and pass: it is the control case proving the balance diagnostic
    does not cry wolf at harmless outcome non-linearity.
    """
    return _sample(n, seed=seed, nonlinear_outcome=True)


def make_propensity_misspecified_data(
    n: int = 600, seed: int = 71
) -> SyntheticData:
    """Non-linear *assignment* mechanism that the linear logistic propensity
    model cannot represent (a quadratic omitted term).

    The true propensity depends on ``x0**2`` plus a linear part; the fitted
    model uses only the linear index, so its weights are biased and fail to
    balance the x0**2 confounder. The weighted-balance diagnostic must flag
    this (it is the misspecification IPW is NOT robust to), while overlap and
    raw score ranges look innocuous.
    """
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, 2))
    eta = 1.2 * (x[:, 0] ** 2 - 1.0) - 0.5 * x[:, 1]
    p = 1.0 / (1.0 + np.exp(-eta))
    t = (rng.uniform(size=n) < p).astype(int)
    # Outcome depends on the omitted quadratic confounder -> real bias if the
    # propensity miss leaves residual imbalance.
    y = (
        INTERCEPT
        + 2.0 * x[:, 0]
        + 1.5 * x[:, 0] ** 2
        - 1.0 * x[:, 1]
        + TRUE_ATE * t
        + rng.normal(scale=0.5, size=n)
    )
    return SyntheticData(
        treatment=t,
        outcome=y,
        covariates=x,
        true_propensity=p,
        true_ate=TRUE_ATE,
        feature_names=("x0", "x1"),
    )


def make_no_overlap_data(n: int = 600, seed: int = 44) -> SyntheticData:
    """Genuine single-arm regions (positivity voids).

    Two dedicated blocks live in a covariate region with *no* counterfactual
    units: high x0 is treated-only and low x0 is control-only. The background
    sample is forced consistently if it falls in those regions, so the blocks
    are real voids, not merely rare tail points.
    """
    n_block = max(20, n // 10)
    rng = np.random.default_rng(seed)
    x_bg = rng.normal(size=(n - 2 * n_block, 2))
    t_bg = (rng.uniform(size=n - 2 * n_block) < 0.5).astype(int)

    x_hi = np.column_stack(
        [rng.uniform(2.3, 3.3, n_block), rng.uniform(-0.5, 0.5, n_block)]
    )
    x_lo = np.column_stack(
        [rng.uniform(-3.3, -2.3, n_block), rng.uniform(-0.5, 0.5, n_block)]
    )
    x = np.vstack([x_bg, x_hi, x_lo])
    t = np.concatenate([t_bg, np.ones(n_block, int), np.zeros(n_block, int)])
    # Enforce the void deterministically across every unit.
    t[x[:, 0] > 2.0] = 1
    t[x[:, 0] < -2.0] = 0

    eta = 0.8 * x[:, 0] - 0.6 * x[:, 1]
    p = 1.0 / (1.0 + np.exp(-eta))
    y = _outcome(t, x) + rng.normal(scale=0.5, size=n)
    return SyntheticData(
        treatment=t, outcome=y, covariates=x, true_propensity=p,
        true_ate=TRUE_ATE, feature_names=("x0", "x1"),
    )


def make_separated_data(n: int = 400, seed: int = 55) -> SyntheticData:
    """Deterministic T|X -> fitted scores hit 0/1 -> weights undefined."""
    return _sample(n, seed=seed, force_separation=True)
