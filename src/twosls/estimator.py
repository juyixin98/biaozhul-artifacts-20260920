"""Estimator orchestration: kernel + diagnostics -> EstimationOutcome.

Status semantics
----------------
* ``ok``           : identified and instruments clear the strength thresholds
* ``weak``         : identified but weak-instrument diagnostics fire; the point
                     estimate is returned with prominent warnings (unless
                     ``strict=true``, in which case the request is rejected)
* ``inconclusive`` : identified formally, but a validity diagnostic (e.g.
                     over-id rejection) makes structural interpretation
                     untenable under the maintained assumptions

``unidentified`` never returns an outcome: it raises ``UnidentifiedError``.
"""
from __future__ import annotations

import numpy as np
from scipy import stats

from .config import Settings, settings as default_settings
from .contract import (
    CoefficientResult,
    EstimationData,
    EstimationOutcome,
    EstimationRequest,
)
from .data import build_data
from .errors import DegenerateDataError, UnidentifiedError, WeakInstrumentError
from .identification import check_identification, first_stage_diagnostics
from .kernel import run_kernel
from .logging_utils import get_logger, log_event
from .validity import endogeneity_test, sargan_test
import logging

logger = get_logger("estimator")


def _coefficient_results(
    names: tuple[str, ...], delta: np.ndarray, vcov: np.ndarray, confidence: float
) -> list[CoefficientResult]:
    se = np.sqrt(np.clip(np.diag(vcov), 0.0, None))
    t = np.divide(delta, se, out=np.full_like(delta, np.nan), where=se > 0)
    # p-values use a normal reference for 2SLS asymptotics
    p = 2.0 * stats.norm.sf(np.abs(t))
    z = stats.norm.ppf(0.5 + confidence / 2.0)
    out: list[CoefficientResult] = []
    for i, name in enumerate(names):
        out.append(
            CoefficientResult(
                name=name,
                estimate=float(delta[i]),
                std_error=float(se[i]),
                t_stat=float(t[i]),
                p_value=float(p[i]),
                ci_low=float(delta[i] - z * se[i]),
                ci_high=float(delta[i] + z * se[i]),
            )
        )
    return out


def _bootstrap_se(data: EstimationData, reps: int, rng: np.random.Generator) -> np.ndarray:
    """Paired nonparametric bootstrap of the 2SLS coefficient vector."""
    n = data.nobs
    r = data.n_endog + data.n_exog
    draws = np.empty((reps, r))
    kept = 0
    attempts = 0
    # Resample until ``reps`` finite fits are obtained (bootstrap samples can
    # themselves be rank-deficient with weak instruments).
    while kept < reps and attempts < reps * 10:
        attempts += 1
        idx = rng.integers(0, n, size=n)

        class _View:
            request_id = data.request_id
            y = data.y[idx]
            Y = data.Y[idx]
            X = data.X[idx]
            Z = data.Z[idx]
            nobs = n
            n_endog = data.n_endog
            n_exog = data.n_exog
            n_instruments = data.n_instruments
            exog_names = data.exog_names
            endog_names = data.endog_names
            instrument_names = data.instrument_names

        try:
            art = run_kernel(_View(), covariance="homoskedastic")  # type: ignore[arg-type]
        except Exception:
            continue
        if np.all(np.isfinite(art.delta)):
            draws[kept] = art.delta
            kept += 1
    if kept < reps:
        draws = draws[:kept]
    if kept < max(20, reps // 5):
        raise UnidentifiedError(
            f"bootstrap produced only {kept}/{reps} finite refits "
            "(design is too fragile to resample)",
            request_id=data.request_id,
            key_state={"bootstrap_kept": kept, "bootstrap_reps": reps},
        )
    return draws.std(axis=0, ddof=1)


def estimate(
    request: EstimationRequest,
    cfg: Settings = default_settings,
    rng: np.random.Generator | None = None,
) -> EstimationOutcome:
    rid = request.request_id
    data = build_data(request)
    log_event(logger, logging.INFO, rid, "data_validated", **data.to_log_state())

    first_stages = first_stage_diagnostics(data)
    ident = check_identification(data, first_stages, cfg)
    log_event(
        logger,
        logging.INFO,
        rid,
        "identification_checked",
        status=ident.status,
        rank=ident.rank_value,
        cragg_donald=ident.cragg_donald_statistic,
        reasons=ident.reasons,
    )

    if ident.status == "unidentified":
        raise UnidentifiedError(
            "model is not identified; 2SLS point estimate is not defined",
            request_id=rid,
            key_state={
                **data.to_log_state(),
                "rank": ident.rank_value,
                "rank_required": ident.rank_required,
                "reasons": ident.reasons,
            },
        )

    try:
        art = run_kernel(data, covariance=request.options.covariance, request_id=rid)
    except DegenerateDataError as exc:
        # Rank diagnostics should already have caught this; convert any that
        # slips through (e.g. an exactly singular IV cross-product) into the
        # same stable NOT_IDENTIFIED verdict rather than a 500.
        raise UnidentifiedError(
            "IV normal-equation matrix is singular; model is not identified",
            request_id=rid,
            key_state={**data.to_log_state(), "kernel_state": exc.key_state},
        ) from exc

    overid = sargan_test(art, data, robust=request.options.covariance == "robust")
    endog = endogeneity_test(art, data, robust=request.options.covariance == "robust")

    names = data.regressor_names()
    coefs = _coefficient_results(names, art.delta, art.vcov, request.options.confidence_level)

    warnings = list(ident.reasons)
    status = "ok" if ident.status == "identified" else "weak"

    bootstrap_ses: np.ndarray | None = None
    if request.options.bootstrap_reps > 0:
        bootstrap_ses = _bootstrap_se(
            data, request.options.bootstrap_reps, rng or np.random.default_rng(0xC0FFEE)
        )
        coefs = [
            CoefficientResult(
                name=c.name,
                estimate=c.estimate,
                std_error=c.std_error,
                t_stat=c.t_stat,
                p_value=c.p_value,
                ci_low=c.ci_low,
                ci_high=c.ci_high,
                std_error_bootstrap=float(bse),
            )
            for c, bse in zip(coefs, bootstrap_ses)
        ]

    if overid.verdict == "reject":
        status = "inconclusive"
        warnings.append(
            f"{overid.test_name} over-id test rejects (p={overid.p_value:.4f}): "
            "structural interpretation invalid under maintained exogeneity"
        )

    if request.options.strict and status == "weak":
        raise WeakInstrumentError(
            "weak instruments and strict=true; estimate withheld",
            request_id=rid,
            key_state={
                **data.to_log_state(),
                "cragg_donald": ident.cragg_donald_statistic,
                "min_first_stage_f": min(f.effective_f_statistic for f in first_stages),
                "reasons": ident.reasons,
            },
        )

    assumptions = {
        "exclusion_restriction": {
            "asserted_by_caller": request.validity_claim.exclusion_restriction_asserted,
            "rationale": request.validity_claim.rationale,
            "derivable_from_data": False,
            "note": (
                "Cov(Z,e)=0 is an economic assumption. First-stage relevance and "
                "(when L>k) over-id tests neither prove nor substitute for it."
            ),
        },
        "instrument_relevance": {
            "testable": True,
            "first_stage_f_threshold": cfg.weak_f_threshold,
            "partial_r2_threshold": cfg.partial_r2_low,
        },
        "error_covariance": request.options.covariance,
    }

    outcome = EstimationOutcome(
        request_id=rid,
        status=status,
        coefficients=coefs,
        first_stage=first_stages,
        identification=ident,
        overidentification=overid,
        endogeneity=endog,
        assumptions=assumptions,
        covariance_kind=request.options.covariance,
        nobs=data.nobs,
        n_endog=data.n_endog,
        n_exog=data.n_exog,
        n_instruments=data.n_instruments,
        degrees_of_freedom_resid=data.nobs - (data.n_endog + data.n_exog),
        sigma2=art.sigma2,
        warnings=tuple(warnings),
    )
    log_event(
        logger,
        logging.INFO,
        rid,
        "estimation_complete",
        status=status,
        sigma2=art.sigma2,
        n_warnings=len(warnings),
    )
    return outcome
