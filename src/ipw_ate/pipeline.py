"""System-boundary validation and end-to-end orchestration.

Nothing here trusts caller data: shapes, finiteness, binary treatment and
sufficient per-arm/per-fold counts are checked before the kernel runs.
"""

from __future__ import annotations

import numpy as np

from .contract import Decision, EstimateResult, IPWConfig, ObservationSet
from .diagnostics import build_diagnostic
from .errors import (
    DataValidationError,
    InsufficientDataError,
    OverlapViolationError,
)
from .estimator import estimate_ate
from .propensity import cross_fit_propensity
from .weights import build_weights


def validate_observations(
    treatment: np.ndarray,
    outcome: np.ndarray,
    covariates: np.ndarray,
    feature_names: tuple[str, ...] | None = None,
) -> ObservationSet:
    """Validate raw arrays into an immutable :class:`ObservationSet`."""
    t = np.asarray(treatment, dtype=float)
    y = np.asarray(outcome, dtype=float)
    x = np.asarray(covariates, dtype=float)

    if t.ndim != 1 or y.ndim != 1:
        raise DataValidationError("treatment and outcome must be 1-D")
    if x.ndim != 2:
        raise DataValidationError("covariates must be 2-D (n, p)")
    if not (t.shape[0] == y.shape[0] == x.shape[0]):
        raise DataValidationError("treatment/outcome/covariates length mismatch")
    n = t.shape[0]
    if n < 4:
        raise InsufficientDataError(f"need at least 4 units, got n={n}")
    if x.shape[1] == 0:
        raise DataValidationError("at least one covariate is required")

    if not np.all(np.isfinite(t)) or not np.all(np.isfinite(y)) or not np.all(
        np.isfinite(x)
    ):
        raise DataValidationError("non-finite values in inputs")
    if not np.all((t == 0) | (t == 1)):
        raise DataValidationError("treatment must be binary {0,1}")

    names = feature_names or tuple(f"x{j}" for j in range(x.shape[1]))
    if len(names) != x.shape[1]:
        raise DataValidationError("feature_names length != number of covariates")

    return ObservationSet(
        treatment=t.astype(int), outcome=y, covariates=x, feature_names=tuple(names)
    )


def run_ipw(
    treatment: np.ndarray,
    outcome: np.ndarray,
    covariates: np.ndarray,
    config: IPWConfig | None = None,
    request_id: str = "local",
    feature_names: tuple[str, ...] | None = None,
    ci_level: float = 0.95,
) -> EstimateResult:
    """Validate -> cross-fit propensity -> stable weights -> ATE -> diagnostics.

    The diagnostic decision gates interpretation but is always attached; a
    REJECT is also raised as :class:`OverlapViolationError` so callers cannot
    accidentally consume an unsupported point estimate without explicitly
    catching it.
    """
    cfg = config or IPWConfig()
    data = validate_observations(treatment, outcome, covariates, feature_names)

    n1 = int(data.treatment.sum())
    n0 = data.n - n1
    if n1 < cfg.n_splits or n0 < cfg.n_splits:
        raise InsufficientDataError(
            f"need >= n_splits={cfg.n_splits} units per arm; got "
            f"treated={n1} control={n0}"
        )

    propensity, fold_ids, folds = cross_fit_propensity(
        data.treatment, data.covariates, cfg
    )
    weights = build_weights(data.treatment, propensity, cfg)
    tau, se, lo, hi = estimate_ate(
        data.treatment, data.outcome, weights, cfg.estimand, ci_level
    )
    diagnostic = build_diagnostic(
        data, weights, propensity, fold_ids, folds, cfg, request_id
    )

    result = EstimateResult(
        estimand=cfg.estimand.value,
        estimate=tau,
        std_error=se,
        ci_lower=lo,
        ci_upper=hi,
        ci_level=ci_level,
        weights=weights,
        propensity=propensity,
        fold_ids=fold_ids,
        diagnostic=diagnostic,
        request_id=request_id,
    )

    if diagnostic.decision is Decision.REJECT:
        raise OverlapViolationError(
            f"[{request_id}] {diagnostic.message}", diagnostic=diagnostic
        )
    return result
