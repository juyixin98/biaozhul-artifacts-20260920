"""Stable IPW weights, fixed truncation, and effective sample size.

Stable (Hájek) weights normalize within arm, so the magnitude does not depend
on the overall treatment fraction. Truncation uses the fixed, declared
probability bounds from :class:`IPWConfig` -- scores are clipped to
``[trim_lower, trim_upper]`` *before* inversion, which is equivalent to
capping each arm's weight at ``1/trim_lower`` / ``1/(1-trim_upper)``.

Target populations
~~~~~~~~~~~~~~~~~~
- ATE: both arms weighted; treated ``1/e``, control ``1/(1-e)``.
- ATT: treated weight ``1`` (the target population), control ``e/(1-e)``.

Near-zero/one scores are handled upstream
(:func:`ipw_ate.propensity._assert_finite_interior`); this module never
substitutes a denominator.
"""

from __future__ import annotations

import numpy as np

from .contract import Estimand, IPWConfig
from .errors import EstimationError


def effective_sample_size(weights: np.ndarray) -> float:
    """Kish effective sample size: (sum w)^2 / sum(w^2).

    Defined as 0.0 for an empty arm or a zero-total-weight arm.
    """
    if weights.size == 0:
        return 0.0
    total = float(np.sum(weights))
    sq = float(np.sum(weights**2))
    if sq <= 0.0:
        return 0.0
    return total * total / sq


def build_weights(
    treatment: np.ndarray,
    propensity: np.ndarray,
    config: IPWConfig,
) -> np.ndarray:
    """Return per-unit stable, trimmed, arm-normalized weights.

    Trimming is fixed (``config.trim_lower``/``trim_upper``), not estimated
    from the realized weight distribution.
    """
    if propensity.shape != treatment.shape:
        raise EstimationError("propensity and treatment shape mismatch")
    # Defensive interior check (scores should already have been validated).
    if np.any(propensity <= 0.0) or np.any(propensity >= 1.0):
        raise EstimationError(
            "propensity score at 0/1 reached weight construction; "
            "weights are undefined"
        )

    e = np.clip(propensity, config.trim_lower, config.trim_upper)
    treated = treatment == 1
    control = ~treated

    raw = np.empty_like(propensity)
    if config.estimand is Estimand.ATE:
        raw[treated] = 1.0 / e[treated]
        raw[control] = 1.0 / (1.0 - e[control])
    elif config.estimand is Estimand.ATT:
        # Treated define the target: unit weight. Controls re-weighted to the
        # treated covariate law via the odds.
        raw[treated] = 1.0
        raw[control] = e[control] / (1.0 - e[control])
    else:  # pragma: no cover - guarded by config validation
        raise EstimationError(f"unsupported estimand {config.estimand!r}")

    return _normalize_within_arm(raw, treated, control)


def _normalize_within_arm(
    raw: np.ndarray, treated: np.ndarray, control: np.ndarray
) -> np.ndarray:
    """Hájek normalization so each arm's weights sum to its own count.

    Normalizing by the in-arm mean (rather than the grand mean) keeps ATE
    arm means on the population scale and makes the treated/control means
    directly comparable. A zero total weight in any arm is undefined.
    """
    out = np.zeros_like(raw)
    for mask in (treated, control):
        arm_w = raw[mask]
        if arm_w.size == 0:
            continue
        total = float(np.sum(arm_w))
        if total <= 0.0:
            raise EstimationError("zero total weight in an arm; mean undefined")
        out[mask] = arm_w * (arm_w.size / total)
    return out
