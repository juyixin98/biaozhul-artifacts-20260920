"""Overlap and weighting diagnostics with recorded, auditable findings.

Every check returns a :class:`DiagnosticFinding` carrying:

* a stable ``code`` tests and clients can assert on,
* a severity (info/warn/reject) and a status,
* human-readable *why*,
* the quantitative evidence behind the decision.

The aggregate verdict follows an explicit, documented rule:
reject > warn > inconclusive > accept. No check silently discards evidence.

Log lines are structured and carry the request/run id plus key state only.
Raw observations, raw covariate values and outcomes are never logged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any, Sequence

import numpy as np
from scipy import stats

from .config import DiagnosticsConfig
from .statcontract import DiagnosticFinding, OverlapSummary, WeightSummary, Verdict
from .weights import WeightResult, effective_sample_size

logger = logging.getLogger("ipwate.diagnostics")


def _quantile(x: np.ndarray, q: float) -> float:
    return float(np.quantile(x, q)) if len(x) else float("nan")


def summarize_overlap(
    a: np.ndarray, p_raw: np.ndarray, p_used: np.ndarray, n_clipped: int, eps: float
) -> OverlapSummary:
    treated = a == 1
    untreated = ~treated
    pt, pu = p_raw[treated], p_raw[untreated]
    n_near = int(np.sum((p_raw <= eps) | (p_raw >= 1.0 - eps)))
    return OverlapSummary(
        pscore_min=float(p_raw.min()),
        pscore_max=float(p_raw.max()),
        treated_q05=_quantile(pt, 0.05),
        treated_q95=_quantile(pt, 0.95),
        untreated_q05=_quantile(pu, 0.05),
        untreated_q95=_quantile(pu, 0.95),
        treated_n=int(treated.sum()),
        untreated_n=int(untreated.sum()),
        n_clipped=int(n_clipped),
        n_near_boundary=n_near,
    )


def summarize_weights(a: np.ndarray, wr: WeightResult) -> WeightSummary:
    treated = a == 1
    untreated = ~treated
    w1, w0 = wr.weights[treated], wr.weights[untreated]
    ess1 = effective_sample_size(w1)
    ess0 = effective_sample_size(w0)
    # Combined ESS for the two-armed contrast (sum of per-arm Kish ESS).
    overall_ess = ess1 + ess0
    n = len(a)
    return WeightSummary(
        n=n,
        treated_ess=ess1,
        untreated_ess=ess0,
        treated_ess_fraction=ess1 / max(len(w1), 1),
        untreated_ess_fraction=ess0 / max(len(w0), 1),
        overall_ess=overall_ess,
        overall_ess_fraction=overall_ess / n,
        max_weight=float(wr.weights.max()),
        mean_weight_treated=float(w1.mean()),
        mean_weight_untreated=float(w0.mean()),
        weight_sum_treated=float(w1.sum()),
        weight_sum_untreated=float(w0.sum()),
        pscore_summary={
            "raw_min": float(wr.p_raw.min()),
            "raw_max": float(wr.p_raw.max()),
            "used_min": float(wr.p_used.min()),
            "used_max": float(wr.p_used.max()),
        },
    )


def _logistic_slope(a: np.ndarray, eta: np.ndarray) -> tuple[float, float, bool]:
    """Univariate logistic regression of A on eta; returns (slope, intercept, ok)."""
    d = np.column_stack([np.ones(len(eta)), eta])
    beta = np.zeros(2)
    for _ in range(100):
        z = np.clip(d @ beta, -60, 60)
        p = 1.0 / (1.0 + np.exp(-z))
        w = np.maximum(p * (1.0 - p), 1e-12)
        grad = d.T @ (p - a) / len(a)
        hess = (d.T * w) @ d / len(a)
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            return float("nan"), float("nan"), False
        beta -= step
        if np.max(np.abs(grad)) < 1e-9:
            return float(beta[1]), float(beta[0]), True
    return float(beta[1]), float(beta[0]), False


def calibration_evidence(
    a: np.ndarray, p: np.ndarray, cfg: DiagnosticsConfig
) -> tuple[dict[str, Any], list[DiagnosticFinding]]:
    """Hosmer-Lemeshow test and calibration slope/intercept.

    Underpowered for small samples: returns an inconclusive finding rather than
    a misleading accept.
    """
    n = len(a)
    groups = cfg.hosmer_lemeshow_groups
    min_n = cfg.calibration_min_n
    findings: list[DiagnosticFinding] = []
    if n < min_n or n < groups * 5:
        finding = DiagnosticFinding(
            code="calibration_sample_too_small",
            severity="info",
            status=Verdict.INCONCLUSIVE.value,
            message=(
                "Calibration cannot be assessed reliably: need at least "
                f"{max(min_n, groups * 5)} observations, got {n}."
            ),
            evidence={"n": n, "required": max(min_n, groups * 5)},
        )
        findings.append(finding)
        return {"assessed": False, "n": n}, findings

    order = np.argsort(p)
    bins = np.array_split(order, groups)
    observed = np.array([a[idx].sum() for idx in bins], dtype=np.float64)
    expected = np.array([p[idx].sum() for idx in bins], dtype=np.float64)
    sizes = np.array([len(idx) for idx in bins], dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        hl = np.sum((observed - expected) ** 2 / (expected * (1.0 - expected / sizes)))
    hl = float(np.nan_to_num(hl, nan=0.0, posinf=0.0))
    hl_p = float(stats.chi2.sf(hl, df=groups - 2))

    eta = np.log(p / (1.0 - p))
    slope, intercept, slope_ok = _logistic_slope(a, eta)
    lo, hi = cfg.calibration_slope_bounds
    evidence = {
        "assessed": True,
        "n": n,
        "hosmer_lemeshow_chi2": hl,
        "hosmer_lemeshow_p": hl_p,
        "calibration_slope": slope,
        "calibration_intercept": intercept,
        "mean_predicted": float(p.mean()),
        "observed_treated_rate": float(a.mean()),
        "slope_bounds": [lo, hi],
        "groups": groups,
    }

    if hl_p < cfg.hosmer_lemeshow_p_min:
        findings.append(
            DiagnosticFinding(
                code="calibration_hl_reject",
                severity="reject",
                status=Verdict.REJECT.value,
                message=(
                    f"Hosmer-Lemeshow p={hl_p:.4g} below "
                    f"{cfg.hosmer_lemeshow_p_min}: propensity scores are miscalibrated; "
                    "IPW weights are unreliable. Consider model misspecification."
                ),
                evidence={"hl_p": hl_p, "hl_chi2": hl, "threshold": cfg.hosmer_lemeshow_p_min},
            )
        )
    elif hl_p < 0.05:
        findings.append(
            DiagnosticFinding(
                code="calibration_hl_warn",
                severity="warn",
                status=Verdict.WARN.value,
                message=f"Hosmer-Lemeshow p={hl_p:.4g} suggests possible miscalibration.",
                evidence={"hl_p": hl_p, "hl_chi2": hl},
            )
        )

    if slope_ok and not (lo <= slope <= hi):
        findings.append(
            DiagnosticFinding(
                code="calibration_slope_warn",
                severity="warn",
                status=Verdict.WARN.value,
                message=(
                    f"Calibration slope {slope:.3f} outside [{lo}, {hi}]: scores are "
                    "over- or under-dispersed relative to observed treatment rates."
                ),
                evidence={"slope": slope, "bounds": [lo, hi]},
            )
        )
    return evidence, findings


def overlap_findings(
    overlap: OverlapSummary, cfg: DiagnosticsConfig
) -> list[DiagnosticFinding]:
    findings: list[DiagnosticFinding] = []
    t_lo, t_hi = overlap.treated_q05, overlap.treated_q95
    u_lo, u_hi = overlap.untreated_q05, overlap.untreated_q95

    if overlap.n_near_boundary > 0:
        findings.append(
            DiagnosticFinding(
                code="pscore_numerical_boundary",
                severity="reject",
                status=Verdict.REJECT.value,
                message=(
                    f"{overlap.n_near_boundary} fitted score(s) at the numerical 0/1 "
                    "boundary (within positivity_eps). Positivity is empirically violated; "
                    "with clipping the estimand is redefined to a trimmed population."
                ),
                evidence={
                    "n_near_boundary": overlap.n_near_boundary,
                    "pscore_min": overlap.pscore_min,
                    "pscore_max": overlap.pscore_max,
                },
            )
        )

    if t_hi <= u_lo or u_hi <= t_lo:
        findings.append(
            DiagnosticFinding(
                code="overlap_none",
                severity="reject",
                status=Verdict.REJECT.value,
                message=(
                    "The central 90% propensity-score intervals of the treated and "
                    "untreated groups do not intersect: there is no common support. "
                    "IPW is not identifiable over the target population."
                ),
                evidence={
                    "treated_q05_q95": [t_lo, t_hi],
                    "untreated_q05_q95": [u_lo, u_hi],
                },
            )
        )
    elif t_lo < u_lo and (t_hi - u_lo) < 0.25 * max(t_hi - t_lo, 1e-12):
        findings.append(
            DiagnosticFinding(
                code="overlap_thin",
                severity="warn",
                status=Verdict.WARN.value,
                message=(
                    "Only a thin region of common support between the groups' central "
                    "90% score intervals; extreme weights are likely."
                ),
                evidence={
                    "treated_q05_q95": [t_lo, t_hi],
                    "untreated_q05_q95": [u_lo, u_hi],
                },
            )
        )

    if overlap.n_clipped > 0:
        findings.append(
            DiagnosticFinding(
                code="fixed_clipping_applied",
                severity="info",
                status=Verdict.WARN.value,
                message=(
                    f"{overlap.n_clipped} score(s) fell outside the FIXED clipping "
                    "profile; weights correspond to the declared trimmed population, "
                    "not the original estimand."
                ),
                evidence={"n_clipped": overlap.n_clipped},
            )
        )
    return findings


def weight_findings(
    ws: WeightSummary, cfg: DiagnosticsConfig
) -> list[DiagnosticFinding]:
    findings: list[DiagnosticFinding] = []
    arm_fractions = {
        "treated": ws.treated_ess_fraction,
        "untreated": ws.untreated_ess_fraction,
    }
    for arm, frac in arm_fractions.items():
        if frac < cfg.min_ess_fraction_reject:
            sev, status, code = "reject", Verdict.REJECT.value, "ess_reject"
            msg = (
                f"{arm} ESS fraction {frac:.3%} below reject threshold "
                f"{cfg.min_ess_fraction_reject:.0%}: effective information is too small."
            )
        elif frac < cfg.min_ess_fraction_warn:
            sev, status, code = "warn", Verdict.WARN.value, "ess_warn"
            msg = (
                f"{arm} ESS fraction {frac:.3%} below warn threshold "
                f"{cfg.min_ess_fraction_warn:.0%}: weights are concentrated on few units."
            )
        else:
            continue
        findings.append(
            DiagnosticFinding(
                code=f"{code}_{arm}",
                severity=sev,
                status=status,
                message=msg,
                evidence={
                    "ess_fraction": frac,
                    "warn_threshold": cfg.min_ess_fraction_warn,
                    "reject_threshold": cfg.min_ess_fraction_reject,
                },
            )
        )

    if ws.max_weight > cfg.max_weight_reject:
        findings.append(
            DiagnosticFinding(
                code="max_weight_reject",
                severity="reject",
                status=Verdict.REJECT.value,
                message=(
                    f"Maximum weight {ws.max_weight:.2f} exceeds reject threshold "
                    f"{cfg.max_weight_reject}: a handful of units dominate the estimate."
                ),
                evidence={"max_weight": ws.max_weight, "threshold": cfg.max_weight_reject},
            )
        )
    elif ws.max_weight > cfg.max_weight_warn:
        findings.append(
            DiagnosticFinding(
                code="max_weight_warn",
                severity="warn",
                status=Verdict.WARN.value,
                message=(
                    f"Maximum weight {ws.max_weight:.2f} exceeds warn threshold "
                    f"{cfg.max_weight_warn}."
                ),
                evidence={"max_weight": ws.max_weight, "threshold": cfg.max_weight_warn},
            )
        )
    return findings


def aggregate_verdict(findings: Sequence[DiagnosticFinding]) -> Verdict:
    statuses = {f.status for f in findings}
    if Verdict.REJECT.value in statuses:
        return Verdict.REJECT
    if Verdict.WARN.value in statuses:
        return Verdict.WARN
    if Verdict.INCONCLUSIVE.value in statuses:
        return Verdict.INCONCLUSIVE
    return Verdict.ACCEPT


def run_diagnostics(
    a: np.ndarray,
    wr: WeightResult,
    cfg: DiagnosticsConfig,
) -> tuple[Verdict, WeightSummary, OverlapSummary, dict[str, Any], list[DiagnosticFinding]]:
    ws = summarize_weights(a, wr)
    ov = summarize_overlap(a, wr.p_raw, wr.p_used, wr.n_clipped, eps=1e-12)
    calibration, cal_findings = calibration_evidence(a, wr.p_used, cfg)
    findings = [*overlap_findings(ov, cfg), *weight_findings(ws, cfg), *cal_findings]
    verdict = aggregate_verdict(findings)
    return verdict, ws, ov, calibration, findings


def redact_for_log(*, n_observations: int, n_covariates: int, n_treated: int) -> dict[str, Any]:
    """Only non-reversibly identifying aggregate state may be logged."""
    return {
        "n_observations": n_observations,
        "n_covariates": n_covariates,
        "n_treated": n_treated,
        "n_untreated": n_observations - n_treated,
    }


def log_run(
    *,
    request_id: str,
    verdict: str,
    redacted: dict[str, Any],
    ws: WeightSummary,
    overlap: OverlapSummary,
    findings: Sequence[DiagnosticFinding],
) -> None:
    """Emit one structured, data-free log line with the run id and key state."""
    payload = {
        "event": "ipw_run_complete",
        "request_id": request_id,
        "verdict": verdict,
        **redacted,
        "overall_ess_fraction": round(ws.overall_ess_fraction, 6),
        "treated_ess_fraction": round(ws.treated_ess_fraction, 6),
        "untreated_ess_fraction": round(ws.untreated_ess_fraction, 6),
        "max_weight": round(ws.max_weight, 6),
        "n_clipped": overlap.n_clipped,
        "n_near_boundary": overlap.n_near_boundary,
        "finding_codes": [f.code for f in findings],
    }
    emit = logger.warning if verdict == "reject" else logger.info
    emit(json.dumps(payload))
