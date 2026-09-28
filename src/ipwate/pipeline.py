"""Cross-fitting pipeline: orchestrates folds, model fits and the IPW kernel.

Declared cross-fitting contract:

1. K stratified folds are built from the declared seed (reproducible).
2. A separate ridge-logistic model is fit on K-1 folds and predicts ONLY the
   held-out fold. Every unit therefore has an out-of-fold (OOF) propensity
   score; no unit's score is produced by a model that saw that unit.
3. Weights and the point estimate are computed once, on the full sample, from
   the assembled OOF score vector.
4. Clipping, if enabled, is the single FIXED profile from the config.
"""

from __future__ import annotations

import uuid

import numpy as np

from .config import AppConfig, load_config
from .diagnostics import log_run, redact_for_log, run_diagnostics
from .model import fit_logistic_ridge
from .splits import make_stratified_folds
from .statcontract import (
    CAUSAL_DISCLAIMER,
    Estimate,
    EstimationResult,
    FoldRecord,
    StatisticalContract,
    Verdict,
)
from .validation import validate_xy
from .weights import compute_weighted_estimation


def cross_fit_propensity(
    x: np.ndarray,
    a: np.ndarray,
    config: AppConfig,
) -> tuple[np.ndarray, list[FoldRecord]]:
    """Return out-of-fold propensity scores plus per-fold audit records."""
    folds = make_stratified_folds(
        a,
        n_splits=config.crossfit.n_splits,
        seed=config.crossfit.seed,
        stratify=config.crossfit.stratify,
    )
    p_oof = np.full(len(a), np.nan, dtype=np.float64)
    records: list[FoldRecord] = []
    for fold in folds:
        model = fit_logistic_ridge(
            x[fold.train_idx],
            a[fold.train_idx],
            ridge_lambda=config.model.ridge_lambda,
            max_iter=config.model.max_iter,
            tol=config.model.tol,
        )
        p_oof[fold.eval_idx] = model.predict_proba(x[fold.eval_idx])
        a_train = a[fold.train_idx]
        records.append(
            FoldRecord(
                fold=fold.fold,
                n_train=len(fold.train_idx),
                n_eval=len(fold.eval_idx),
                n_train_treated=int(a_train.sum()),
                n_train_untreated=int(len(a_train) - a_train.sum()),
                converged=model.converged,
                n_iter=model.n_iter,
            )
        )
    if np.any(np.isnan(p_oof)):
        # Defensive: folds partition the sample, so this indicates a bug.
        raise RuntimeError("Out-of-fold scores were not assigned to every unit")
    return p_oof, records


def run_ipw(
    x: np.ndarray | list[list[float]],
    a: np.ndarray | list[float],
    y: np.ndarray | list[float],
    *,
    config: AppConfig | None = None,
    overrides: dict | None = None,
    request_id: str | None = None,
    persist: bool = False,
) -> EstimationResult:
    """Validate -> cross-fit -> weight -> diagnose -> assemble result.

    ``overrides`` may contain only the API-whitelisted contract fields.
    Positivity / overlap rejections are returned INSIDE the result with a
    REJECT verdict when the evidence pipeline can build one; a raw positivity
    failure with clipping off still raises :class:`PositivityError`, which the
    service maps to a structured error.
    """
    config = (config or load_config()).with_api_overrides(overrides or {})
    request_id = request_id or f"run-{uuid.uuid4()}"

    x_arr, a_arr, y_arr = validate_xy(
        x,
        a,
        y,
        max_observations=config.api.max_observations,
        max_covariates=config.api.max_covariates,
    )

    p_oof, fold_records = cross_fit_propensity(x_arr, a_arr, config)
    contract = StatisticalContract.from_config(config)

    wr = compute_weighted_estimation(
        a_arr,
        y_arr,
        p_oof,
        estimand=config.estimand,
        weight_type=config.weight_type,
        clipping=config.weights.clipping,
        positivity_eps=config.weights.positivity_eps,
    )

    verdict, ws, overlap, calibration, findings = run_diagnostics(
        a_arr, wr, config.diagnostics
    )
    estimate = Estimate(
        point=wr.point,
        se=wr.se,
        ci_level=0.95,
        ci_lower=wr.ci_lower,
        ci_upper=wr.ci_upper,
        mu1=wr.mu1,
        mu0=wr.mu0,
        influence_used=True,
    )
    warnings_list = [f.message for f in findings if f.severity == "warn"]

    result = EstimationResult(
        request_id=request_id,
        contract=contract,
        verdict=verdict.value,
        estimate=estimate,
        weights=ws,
        overlap=overlap,
        calibration=calibration,
        findings=findings,
        folds=fold_records,
        n_observations=len(a_arr),
        n_covariates=x_arr.shape[1],
        warnings=warnings_list,
        causal_disclaimer=CAUSAL_DISCLAIMER,
    )

    log_run(
        request_id=request_id,
        verdict=verdict.value,
        redacted=redact_for_log(
            n_observations=len(a_arr),
            n_covariates=x_arr.shape[1],
            n_treated=int(a_arr.sum()),
        ),
        ws=ws,
        overlap=overlap,
        findings=findings,
    )

    if persist:
        from .storage import save_run

        save_run(config.storage.db_path, result)
    return result
