"""Application service: orchestrates validation, kernel, diagnostics, records.

This is the single place that knows the end-to-end estimation flow. The HTTP
layer and the demo script both call :func:`run_estimation`.
"""

from __future__ import annotations

import logging

import numpy as np

from .config import ServiceConfig
from .contracts import (
    CoefficientResult,
    DecisionRecord,
    DiagnosticResult,
    EstimateRequest,
    EstimateResponse,
    FirstStageResult,
)
from .data_prep import prepare_data
from .diagnostics import build_assumptions, build_decision, build_diagnostics
from .kernel import KernelResult, estimate as kernel_estimate
from .logging_context import payload_fingerprint

logger = logging.getLogger("twosls.service")


def _coefficients(result: KernelResult) -> list[CoefficientResult]:
    return [
        CoefficientResult(
            name=c.name, estimate=c.estimate, std_error=c.std_error,
            z_stat=c.z_stat, p_value=c.p_value,
            ci_lower=c.ci_lower, ci_upper=c.ci_upper,
        )
        for c in result.coefficients
    ]


def _first_stages(result: KernelResult) -> list[FirstStageResult]:
    return [
        FirstStageResult(
            endogenous_name=fs.endogenous_name,
            partial_f_stat=fs.f_stat,
            partial_f_pvalue=fs.f_pvalue,
            partial_r_squared=fs.partial_r2,
            partial_r_squared_adjusted=fs.partial_r2_adj,
            coefficients=fs.coefficients,
        )
        for fs in result.first_stage
    ]


def _diagnostics(diags) -> list[DiagnosticResult]:
    return [
        DiagnosticResult(
            name=d.name, value=d.value, p_value=d.p_value,
            status=d.status, detail=d.detail,
        )
        for d in diags
    ]


def _build_response(
    result: KernelResult,
    diagnostics,
    decision: DecisionRecord,
    *,
    request_id: str,
    n_obs: int,
    n_user_exogenous: int,
) -> EstimateResponse:
    status = (
        "estimated_weak"
        if decision.failure_category.value == "weak_instruments"
        else "estimated"
    )
    return EstimateResponse(
        request_id=request_id,
        status=status,
        n_obs=n_obs,
        n_endogenous=result.rank.n_endogenous,
        n_instruments=result.rank.n_excluded,
        n_exogenous=n_user_exogenous,
        coefficients=_coefficients(result),
        first_stage=_first_stages(result),
        diagnostics=_diagnostics(diagnostics),
        decision=decision,
    )


def _kernel_from_prepared(prepared, req, config) -> KernelResult:
    return kernel_estimate(
        prepared.y, prepared.x, prepared.w, prepared.z,
        names_endog=prepared.names_endog,
        names_exog=prepared.names_exog,
        names_instruments=prepared.names_instruments,
        add_constant=req.add_constant,
        cov_type=req.cov_type,
        alpha=config.significance,
        rank_rcond=config.rank_rcond,
        run_overid=req.run_overid,
        run_endogeneity=req.run_endogeneity,
    )


def run_estimation(req: EstimateRequest, config: ServiceConfig) -> EstimateResponse:
    """Validate, estimate, diagnose, and assemble the response contract."""
    prepared = prepare_data(req, max_obs=config.max_obs)
    fingerprint = payload_fingerprint(
        req.columns, prefix_len=config.data_hash_prefix_len
    )
    request_id = req.request_id or "-"

    logger.info(
        "estimating request n=%d k=%d l=%d j=%d cov=%s fp=%s declared_exclusion=%s",
        prepared.n_obs, prepared.x.shape[1], prepared.z.shape[1],
        prepared.w.shape[1], req.cov_type, fingerprint,
        req.assume_exclusion_restriction,
    )

    result = _kernel_from_prepared(prepared, req, config)
    hetero_predictors = _hetero_predictors(prepared.w, prepared.z,
                                           req.add_constant)
    diagnostics = build_diagnostics(result, hetero_predictors,
                                    config.overid_significance)
    assumptions = build_assumptions(
        result,
        exclusion_declared=req.assume_exclusion_restriction,
        exclusion_rationale=req.exclusion_rationale,
    )
    decision = build_decision(
        result, diagnostics, assumptions,
        request_id=request_id, fingerprint=fingerprint,
        vif_warn=config.vif_warn,
    )
    logger.info(
        "verdict=%s failure=%s cd_f=%.4f reasons=%d",
        decision.verdict.value, decision.failure_category.value,
        result.cragg_donald, len(decision.reasons),
    )

    return _build_response(
        result, diagnostics, decision, request_id=request_id,
        n_obs=prepared.n_obs,
        n_user_exogenous=prepared.w.shape[1] - (1 if req.add_constant else 0),
    )


def _hetero_predictors(w: np.ndarray, z: np.ndarray, add_constant: bool) -> np.ndarray:
    """BP predictors exclude the implicit constant (the test adds its own)."""
    w_nonconst = w[:, 1:] if add_constant else w
    if w_nonconst.shape[1] == 0:
        return z
    return np.hstack([w_nonconst, z])
