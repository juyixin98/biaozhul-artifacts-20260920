"""Evidence and diagnostics: turn kernel statistics into an explained verdict.

The output deliberately separates three things:

1. **Evidence** (numbers from the data): first-stage F, Cragg-Donald, VIF,
   Sargan/Hansen J, DWH, heteroskedasticity / residual diagnostics.
2. **Assumptions** (the caller's declarations): the exclusion restriction is
   stored as *declared*, never upgraded to "supported" because of a high
   correlation or a large F. Relevance is necessary for, and says nothing
   about, exogeneity.
3. **Decision** (accepted / rejected / inconclusive) with human-readable
   reasons and the key state supporting it, keyed by request id and payload
   fingerprint.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import stats

from .contracts import (
    AssumptionRecord,
    AssumptionStatus,
    DecisionRecord,
    DiagnosticResult,
    FailureCategory,
    Verdict,
)
from .errors import (
    IncompatibleShapes,
    InapplicableTest,
    InsufficientObservations,
    NonFiniteData,
    SingularDesign,
    TwoSLSError,
    UnidentifiedModel,
)
from .kernel import KernelResult

# Stock & Yogo (2005), Table 1: Cragg-Donald weak-ID critical values for
# maximal 10% IV relative bias. Keyed (K endogenous regressors, L total
# excluded instruments). Exact-identification and K>3 cells are not tabulated;
# there the Staiger-Stock rule of thumb (10) is used and labelled as such.
STOCK_YOGO_10PCT: dict[tuple[int, int], float] = {
    (1, 2): 19.93, (1, 3): 22.30, (1, 4): 24.58, (1, 5): 26.87,
    (1, 6): 29.18, (1, 7): 31.50, (1, 8): 33.84, (1, 9): 36.19,
    (1, 10): 38.54, (1, 11): 40.88,
    (2, 3): 13.46, (2, 4): 15.20, (2, 5): 16.87, (2, 6): 18.54,
    (2, 7): 20.21, (2, 8): 21.88,
    (3, 4): 11.39, (3, 5): 12.81, (3, 6): 14.23, (3, 7): 15.65,
    (3, 8): 17.07,
}
RULE_OF_THUMB = 10.0
MARGINAL_MULTIPLIER = 1.2  # within 20% above the cutoff -> warning band


def weak_instrument_cutoff(k: int, ell: int) -> tuple[float, str]:
    cv = STOCK_YOGO_10PCT.get((k, ell))
    if cv is not None:
        return cv, "stock_yogo_10pct_relative_bias"
    return RULE_OF_THUMB, "staiger_stock_rule_of_thumb (cell not tabulated)"


# --------------------------------------------------------------------------- #
# Error diagnostics
# --------------------------------------------------------------------------- #

def breusch_pagan(residuals: np.ndarray, predictors: np.ndarray) -> DiagnosticResult:
    """Breusch-Pagan / Cook-Weisberg score test on 2SLS residuals.

    Regresses squared structural residuals on the non-constant exogenous
    regressors and excluded instruments; ``n R^2 ~ chi2(p)``. Rejection
    recommends the robust VCV / Hansen J path.
    """
    n = residuals.shape[0]
    u2 = residuals**2
    p = predictors.shape[1]
    design = np.column_stack([np.ones(n), predictors]) if p else np.ones((n, 1))
    fitted = design @ np.linalg.lstsq(design, u2, rcond=None)[0]
    tss = float(np.sum((u2 - u2.mean()) ** 2))
    ess = float(np.sum((fitted - u2.mean()) ** 2))
    r2 = 0.0 if tss == 0 else max(0.0, ess / tss)
    statistic = n * r2
    pval = float(stats.chi2.sf(statistic, p))
    hetero = pval < 0.05
    detail = (
        f"Breusch-Pagan nR^2={statistic:.3f} on {p} df, p={pval:.4f}: "
        + ("heteroskedasticity detected; prefer cov_type='robust' / Hansen J"
           if hetero
           else "no significant heteroskedasticity at 5%; conventional VCV adequate")
    )
    return DiagnosticResult(
        name="breusch_pagan_heteroskedasticity", value=statistic, p_value=pval,
        status=AssumptionStatus.SUPPORTED if hetero else AssumptionStatus.REJECTED,
        detail=detail,
    )


def residual_diagnostic(residuals: np.ndarray) -> DiagnosticResult:
    """Jarque-Bera normality + location/scale summary of structural residuals."""
    n = residuals.size
    centered = residuals - residuals.mean()
    sd = math.sqrt(float(centered @ centered) / n)
    skew = float(np.mean(centered**3) / sd**3) if sd > 0 else 0.0
    kurt = float(np.mean(centered**4) / sd**4) if sd > 0 else 3.0
    jb = n / 6.0 * (skew**2 + 0.25 * (kurt - 3.0) ** 2)
    p = float(stats.chi2.sf(jb, 2))
    nonnormal = p < 0.05
    detail = (
        f"mean={residuals.mean():.4g}, sd={sd:.4g}, skew={skew:.3f}, "
        f"excess kurtosis={kurt - 3:.3f}, JB p={p:.4f}; "
        + ("residuals non-normal at 5% (exact F tests are asymptotic anyway)"
           if nonnormal else "no normality rejection at 5%")
    )
    return DiagnosticResult(
        name="structural_residual_distribution", value=jb, p_value=p,
        status=AssumptionStatus.REJECTED if nonnormal
        else AssumptionStatus.SUPPORTED,
        detail=detail,
    )


# --------------------------------------------------------------------------- #
# Evidence assembly
# --------------------------------------------------------------------------- #

def _cragg_donald_diagnostic(result: KernelResult) -> DiagnosticResult:
    k, ell = result.rank.n_endogenous, result.rank.n_excluded
    cutoff, source = weak_instrument_cutoff(k, ell)
    weak = result.cragg_donald < cutoff
    return DiagnosticResult(
        name="cragg_donald_weak_identification",
        value=result.cragg_donald,
        p_value=None,
        status=AssumptionStatus.REJECTED if weak else AssumptionStatus.SUPPORTED,
        detail=(
            f"Cragg-Donald F={result.cragg_donald:.3f} vs critical value "
            f"{cutoff:.2f} ({source}); "
            + ("WEAK: 2SLS estimator biased and inference unreliable"
               if weak else "instruments clear the weak-identification cutoff")
        ),
    )


def _first_stage_diagnostics(result: KernelResult) -> list[DiagnosticResult]:
    out: list[DiagnosticResult] = []
    for fs in result.first_stage:
        # Per-endogenous relevance screen: Staiger-Stock 10. The *joint*
        # decision uses Cragg-Donald / Stock-Yogo, not these screens.
        weak_fs = fs.f_stat < RULE_OF_THUMB
        out.append(DiagnosticResult(
            name=f"first_stage_partial_f[{fs.endogenous_name}]",
            value=fs.f_stat, p_value=fs.f_pvalue,
            status=AssumptionStatus.REJECTED if weak_fs
            else AssumptionStatus.SUPPORTED,
            detail=(
                f"partial F={fs.f_stat:.3f} (p={fs.f_pvalue:.4g}), "
                f"partial R2={fs.partial_r2:.4f}; per-endogenous screen "
                f"threshold {RULE_OF_THUMB:.0f} (joint test is Cragg-Donald)"
            ),
        ))
    return out


def _overid_diagnostic(result: KernelResult, alpha: float) -> DiagnosticResult:
    oid = result.overid
    rej = oid.p_value < alpha
    df = result.rank.n_excluded - result.rank.n_endogenous
    return DiagnosticResult(
        name=oid.kind, value=oid.statistic, p_value=oid.p_value,
        status=AssumptionStatus.REJECTED if rej else AssumptionStatus.SUPPORTED,
        detail=(
            f"{oid.kind}={oid.statistic:.3f} (df={df}, p={oid.p_value:.4f}): "
            + ("over-identifying restrictions REJECTED - at least one "
               "instrument appears endogenous/exclusion violated"
               if rej else
               "no rejection of over-identifying restrictions; this is "
               "consistency evidence only, NOT proof of exogeneity")
        ),
    )


def _endogeneity_diagnostic(result: KernelResult) -> DiagnosticResult:
    endo = result.endogeneity
    treated_endo = endo.p_value < 0.05
    return DiagnosticResult(
        name="durbin_wu_hausman_endogeneity",
        value=endo.statistic, p_value=endo.p_value,
        status=AssumptionStatus.SUPPORTED if treated_endo
        else AssumptionStatus.REJECTED,
        detail=(
            f"DWH chi2={endo.statistic:.3f} (p={endo.p_value:.4f}): "
            + ("treat regressor(s) as endogenous - OLS inconsistent"
               if treated_endo else
               "no evidence of endogeneity - OLS/2SLS statistically "
               "indistinguishable at 5%")
        ),
    )


def build_diagnostics(
    result: KernelResult, hetero_predictors: np.ndarray, overid_alpha: float
) -> list[DiagnosticResult]:
    diagnostics: list[DiagnosticResult] = [_cragg_donald_diagnostic(result)]
    diagnostics.extend(_first_stage_diagnostics(result))
    if result.overid is not None:
        diagnostics.append(_overid_diagnostic(result, overid_alpha))
    if not math.isnan(result.endogeneity.statistic):
        diagnostics.append(_endogeneity_diagnostic(result))
    diagnostics.append(breusch_pagan(result.residuals, hetero_predictors))
    diagnostics.append(residual_diagnostic(result.residuals))
    return diagnostics


# --------------------------------------------------------------------------- #
# Assumption records
# --------------------------------------------------------------------------- #

def _relevance_record(result: KernelResult) -> AssumptionRecord:
    rank = result.rank
    return AssumptionRecord(
        name="instrument_relevance_rank_condition",
        status=AssumptionStatus.SUPPORTED,
        detail=(
            f"order condition L={rank.n_excluded}>=K={rank.n_endogenous}; "
            f"rank([W,Z])={rank.rank_design}/{rank.n_included + rank.n_excluded}; "
            f"rank of projected endogenous regressors={rank.rank_first_stage}/"
            f"{rank.n_endogenous}. Rank and relevance are testable and met."
        ),
    )


def _exclusion_record(declared: bool, rationale: str | None) -> AssumptionRecord:
    if declared:
        suffix = f" Rationale supplied by caller: {rationale}" if rationale else ""
        return AssumptionRecord(
            name="exclusion_restriction_E[Z'e]=0",
            status=AssumptionStatus.DECLARED,
            detail=(
                "Caller DECLARES the exclusion restriction (excluded instruments "
                "affect y only through X). This cannot be inferred from "
                "instrument-outcome correlations or first-stage strength and is "
                "not verified by the service; under exact identification it is "
                "untestable." + suffix
            ),
        )
    return AssumptionRecord(
        name="exclusion_restriction_E[Z'e]=0",
        status=AssumptionStatus.NOT_ASSESSED,
        detail=(
            "Caller did NOT declare the exclusion restriction. 2SLS results "
            "are reported as arithmetic output only; no causal interpretation "
            "is supported. Relevance diagnostics do not establish exclusion."
        ),
    )


def build_assumptions(
    result: KernelResult,
    *,
    exclusion_declared: bool,
    exclusion_rationale: str | None,
) -> list[AssumptionRecord]:
    return [
        _relevance_record(result),
        _exclusion_record(exclusion_declared, exclusion_rationale),
        AssumptionRecord(
            name="linearity_additive_errors",
            status=AssumptionStatus.DECLARED,
            detail="Linear structural model y = W gamma + X beta + e assumed by request.",
        ),
    ]


# --------------------------------------------------------------------------- #
# Decision
# --------------------------------------------------------------------------- #

def _build_key_state(result: KernelResult, cutoff: float, source: str) -> dict:
    state = {
        "n_obs": result.n_obs,
        "n_endogenous": result.rank.n_endogenous,
        "n_excluded_instruments": result.rank.n_excluded,
        "rank_design": result.rank.rank_design,
        "rank_first_stage": result.rank.rank_first_stage,
        "cragg_donald_f": round(result.cragg_donald, 6),
        "weak_cutoff": cutoff,
        "cutoff_source": source,
        "max_instrument_vif": round(result.rank.max_vif, 4),
        "cov_type": result.cov_type,
    }
    if result.overid is not None:
        state["overid_p"] = round(result.overid.p_value, 6)
    if not math.isnan(result.endogeneity.p_value):
        state["dwh_endogeneity_p"] = round(result.endogeneity.p_value, 6)
    return state


def _adjudicate_strength(
    result: KernelResult, cutoff: float, source: str
) -> tuple[Verdict, FailureCategory, str | None]:
    cd = result.cragg_donald
    if cd < cutoff:
        return (
            Verdict.INCONCLUSIVE,
            FailureCategory.WEAK_INSTRUMENTS,
            f"Cragg-Donald F={cd:.3f} below weak-ID cutoff {cutoff:.2f} "
            f"({source}); estimates reported but biased and normal-inference "
            "diagnostics unreliable",
        )
    if cd < MARGINAL_MULTIPLIER * cutoff:
        band = int((MARGINAL_MULTIPLIER - 1) * 100)
        return (
            Verdict.ACCEPTED_WITH_WARNING,
            FailureCategory.NONE,
            f"Cragg-Donald F={cd:.3f} clears cutoff {cutoff:.2f} but sits "
            f"inside the {band}% marginal band; interpret with caution",
        )
    return Verdict.ACCEPTED, FailureCategory.NONE, None


def _apply_collinearity(
    verdict: Verdict, failure: FailureCategory, reasons: list[str],
    max_vif: float, vif_warn: float,
) -> tuple[Verdict, FailureCategory]:
    if max_vif <= vif_warn:
        return verdict, failure
    if verdict is Verdict.ACCEPTED:
        verdict = Verdict.ACCEPTED_WITH_WARNING
    if failure is FailureCategory.NONE:
        failure = FailureCategory.COLLINEAR_INSTRUMENTS
    reasons.append(
        f"excluded instruments are highly collinear (max VIF={max_vif:.2f} "
        f"> {vif_warn}); rank survives but independent instrument information "
        "is thin"
    )
    return verdict, failure


def _apply_overid_warning(
    verdict: Verdict, reasons: list[str], diagnostics: list[DiagnosticResult]
) -> Verdict:
    rejected = any(
        d.name in {"sargan", "hansen_j"}
        and d.status is AssumptionStatus.REJECTED
        for d in diagnostics
    )
    if rejected:
        if verdict is Verdict.ACCEPTED:
            verdict = Verdict.ACCEPTED_WITH_WARNING
        reasons.append(
            "over-identification test rejected: instrument validity assumption "
            "is in tension with the data (see assumptions; exclusion was declared)"
        )
    return verdict


def build_decision(
    result: KernelResult,
    diagnostics: list[DiagnosticResult],
    assumptions: list[AssumptionRecord],
    *,
    request_id: str,
    fingerprint: str,
    vif_warn: float,
) -> DecisionRecord:
    k, ell = result.rank.n_endogenous, result.rank.n_excluded
    cutoff, source = weak_instrument_cutoff(k, ell)
    key_state = _build_key_state(result, cutoff, source)

    verdict, failure, reason = _adjudicate_strength(result, cutoff, source)
    reasons = [reason] if reason else []
    verdict, failure = _apply_collinearity(
        verdict, failure, reasons, result.rank.max_vif, vif_warn
    )
    verdict = _apply_overid_warning(verdict, reasons, diagnostics)

    if verdict is Verdict.ACCEPTED:
        reasons.append(
            f"model identified (L={ell}>=K={k}, full rank), Cragg-Donald "
            f"F={result.cragg_donald:.3f} >= {cutoff:.2f}, no collinearity trigger"
        )

    return DecisionRecord(
        request_id=request_id, fingerprint=fingerprint, n_obs=result.n_obs,
        verdict=verdict, failure_category=failure, reasons=reasons,
        key_state=key_state, assumptions=assumptions,
    )


# --------------------------------------------------------------------------- #
# Error classification (used by the service boundary)
# --------------------------------------------------------------------------- #

ERROR_TO_CATEGORY: dict[type[TwoSLSError], FailureCategory] = {
    InsufficientObservations: FailureCategory.INSUFFICIENT_OBSERVATIONS,
    NonFiniteData: FailureCategory.NON_FINITE_DATA,
    IncompatibleShapes: FailureCategory.INVALID_REQUEST,
    InapplicableTest: FailureCategory.INAPPLICABLE_TEST,
    SingularDesign: FailureCategory.SINGULAR_DESIGN,
}


def classify_failure(exc: Exception) -> tuple[FailureCategory, str]:
    if isinstance(exc, UnidentifiedModel):
        msg = str(exc)
        if "order condition" in msg:
            return FailureCategory.UNDERIDENTIFIED, msg
        if "rank condition" in msg:
            return FailureCategory.WEAK_IDENTIFICATION, msg
        return FailureCategory.WEAK_IDENTIFICATION, msg
    for err_type, category in ERROR_TO_CATEGORY.items():
        if isinstance(exc, err_type):
            return category, str(exc)
    return FailureCategory.INVALID_REQUEST, str(exc)
