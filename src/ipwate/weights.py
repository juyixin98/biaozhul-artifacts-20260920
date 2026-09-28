"""IPW weight and estimator kernel.

This is the mathematical core; it takes *already out-of-fold* propensity
scores and returns weights, weighted means, the point estimate and an
influence-function standard error. Keeping it score-in/numbers-out makes the
formulas independently testable against hand-computed small samples (see
``tests/fixtures/tiny_weights.json`` and ``tests/test_weights_kernel.py``).

Contract rules implemented here:

* a fitted score that makes a relevant denominator <= ``positivity_eps``
  RAISES :class:`PositivityError` when clipping is off — there is no silent
  denominator replacement;
* clipping uses a single FIXED profile and happens before weights are formed,
  so the changed (trimmed-population) estimand is recorded upstream;
* stabilized weights and the target population (ATE/ATT/ATU) are explicit.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats

from .config import ClippingConfig
from .errors import PositivityError

Z_975 = float(stats.norm.ppf(0.975))


@dataclass(frozen=True)
class WeightResult:
    weights: np.ndarray          # full-length vector; zero off the unit's arm usage
    p_used: np.ndarray           # scores actually used (post fixed clipping)
    p_raw: np.ndarray
    n_clipped: int
    near_boundary: dict[str, int]
    marginal_treated: float      # e_bar = P_n(A=1), stabilization numerator
    mu1: float
    mu0: float
    point: float
    se: float
    ci_lower: float
    ci_upper: float
    influence: np.ndarray


def _near_boundary_counts(
    a: np.ndarray, p: np.ndarray, estimand: str, eps: float
) -> dict[str, int]:
    """Count scores within eps of the boundary that is *relevant to the estimand*.

    ATE needs both denominators; ATT only needs 1-p on untreated units (treated
    get weight 1); ATU only needs p on treated units.
    """
    treated = a == 1
    untreated = ~treated
    need_p = estimand in ("ate", "atu")
    need_one_minus_p = estimand in ("ate", "att")
    return {
        "treated_p_le_eps": int(np.sum(treated & (p <= eps))) if need_p else 0,
        "untreated_one_minus_p_le_eps": int(np.sum(untreated & ((1.0 - p) <= eps)))
        if need_one_minus_p
        else 0,
        # informational, never estimand-relevant:
        "treated_p_ge_one_minus_eps": int(np.sum(treated & (p >= 1.0 - eps))),
        "untreated_p_le_eps_info": int(np.sum(untreated & (p <= eps))),
    }


def check_positivity(
    a: np.ndarray,
    p: np.ndarray,
    estimand: str,
    eps: float,
    clipping_enabled: bool,
) -> dict[str, int]:
    """Enforce the no-silent-denominator-replacement rule.

    With clipping disabled, any estimand-relevant denominator <= eps raises.
    With clipping enabled the replacement is *declared* (fixed profile,
    estimand redefinition recorded on the contract), so the caller proceeds;
    the counts are still reported and the diagnostics layer rejects when the
    raw fit hit the numerical boundary.
    """
    counts = _near_boundary_counts(a, p, estimand, eps)
    relevant = counts["treated_p_le_eps"] + counts["untreated_one_minus_p_le_eps"]
    if relevant > 0 and not clipping_enabled:
        raise PositivityError(
            "Fitted propensity score yields a denominator not greater than "
            "positivity_eps for a unit that the estimand needs; denominator "
            "replacement is forbidden by the contract.",
            details={
                "estimand": estimand,
                "positivity_eps": eps,
                "treated_p_le_eps": counts["treated_p_le_eps"],
                "untreated_one_minus_p_le_eps": counts["untreated_one_minus_p_le_eps"],
                "rescue": "Enable the FIXED clipping profile to estimate over a "
                "declared trimmed population (the raw-score failure is still recorded).",
            },
        )
    return counts


def apply_fixed_clipping(
    p: np.ndarray, clipping: ClippingConfig
) -> tuple[np.ndarray, int]:
    p_used = np.clip(p, clipping.lower, clipping.upper)
    n_clipped = int(np.sum((p < clipping.lower) | (p > clipping.upper)))
    return p_used, n_clipped


def _weights_for(
    a: np.ndarray,
    p: np.ndarray,
    e_bar: float,
    estimand: str,
    weight_type: str,
) -> np.ndarray:
    treated = a == 1
    untreated = ~treated
    w = np.zeros(len(a), dtype=np.float64)

    if estimand == "ate":
        if weight_type == "stabilized":
            w[treated] = e_bar / p[treated]
            w[untreated] = (1.0 - e_bar) / (1.0 - p[untreated])
        else:  # Horvitz-Thompson
            w[treated] = 1.0 / p[treated]
            w[untreated] = 1.0 / (1.0 - p[untreated])
    elif estimand == "att":
        # Treated are the target: weight 1.
        w[treated] = 1.0
        ratio = p[untreated] / (1.0 - p[untreated])
        w[untreated] = ratio if weight_type == "ht" else ratio * (1.0 - e_bar) / e_bar
    elif estimand == "atu":
        # Untreated are the target: weight 1.
        w[untreated] = 1.0
        ratio = (1.0 - p[treated]) / p[treated]
        w[treated] = ratio if weight_type == "ht" else ratio * e_bar / (1.0 - e_bar)
    else:  # validated upstream, kept as a defensive guard
        raise ValueError(f"unknown estimand {estimand!r}")
    return w


def _influence_se(
    a: np.ndarray,
    y: np.ndarray,
    p: np.ndarray,
    e_bar: float,
    mu1: float,
    mu0: float,
    estimand: str,
) -> tuple[np.ndarray, float]:
    """Influence-function based SE for the Hajek (self-normalized) IPW mean."""
    treated = a == 1
    untreated = ~treated
    psi = np.zeros(len(a), dtype=np.float64)
    if estimand == "ate":
        psi[treated] = (y[treated] - mu1) / p[treated]
        psi[untreated] = -(y[untreated] - mu0) / (1.0 - p[untreated])
        scale = 1.0
    elif estimand == "att":
        psi[treated] = y[treated] - mu1
        psi[untreated] = -(p[untreated] / (1.0 - p[untreated])) * (
            y[untreated] - mu0
        )
        scale = e_bar
    else:  # atu
        psi[treated] = ((1.0 - p[treated]) / p[treated]) * (y[treated] - mu1)
        psi[untreated] = -(y[untreated] - mu0)
        scale = 1.0 - e_bar
    psi = psi / scale
    se = float(np.std(psi, ddof=1) / np.sqrt(len(a)))
    return psi, se


def effective_sample_size(weights: np.ndarray) -> float:
    """Kish ESS = (sum w)^2 / sum w^2 (scale invariant)."""
    total = float(np.sum(weights))
    sq = float(np.sum(weights**2))
    if sq <= 0.0:
        return 0.0
    return total**2 / sq


def compute_weighted_estimation(
    a: np.ndarray,
    y: np.ndarray,
    p_raw: np.ndarray,
    *,
    estimand: str,
    weight_type: str,
    clipping: ClippingConfig,
    positivity_eps: float,
) -> WeightResult:
    """Full kernel: positivity guard -> fixed clipping -> weights -> estimate."""
    near = check_positivity(
        a, p_raw, estimand, positivity_eps, clipping.enabled
    )
    if clipping.enabled:
        p_used, n_clipped = apply_fixed_clipping(p_raw, clipping)
    else:
        p_used, n_clipped = p_raw.copy(), 0

    e_bar = float(np.mean(a))
    w = _weights_for(a, p_used, e_bar, estimand, weight_type)

    treated = a == 1
    untreated = ~treated
    sum_w1 = float(np.sum(w[treated]))
    sum_w0 = float(np.sum(w[untreated]))
    if sum_w1 <= 0.0 or sum_w0 <= 0.0:
        # Defensive: positivity checks should make this unreachable.
        raise PositivityError(
            "Degenerate weight sums after weighting",
            details={"sum_w_treated": sum_w1, "sum_w_untreated": sum_w0},
        )
    mu1 = float(np.sum(w[treated] * y[treated]) / sum_w1)
    mu0 = float(np.sum(w[untreated] * y[untreated]) / sum_w0)
    point = mu1 - mu0

    psi, se = _influence_se(a, y, p_used, e_bar, mu1, mu0, estimand)
    return WeightResult(
        weights=w,
        p_used=p_used,
        p_raw=p_raw,
        n_clipped=n_clipped,
        near_boundary=near,
        marginal_treated=e_bar,
        mu1=mu1,
        mu0=mu0,
        point=float(point),
        se=se,
        ci_lower=float(point - Z_975 * se),
        ci_upper=float(point + Z_975 * se),
        influence=psi,
    )
