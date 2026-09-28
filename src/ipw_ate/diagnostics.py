"""Overlap (positivity) evidence and the accept/reject/inconclusive rules.

Every run produces an evidence packet :class:`DiagnosticResult` carrying a
request id and the key aggregate state. The decision is a pure, declared
function of that state:

REJECT (estimation is not trustworthy / undefined)
    - an arm is empty
    - a boundary (0/1) or non-finite score (defensive; usually raised earlier)
    - a *support void*: a cluster of units lies beyond the opposite arm's
      observed support on a covariate (direct positivity failure)
    - effective sample size below ``min_ess_per_arm`` in either arm

INCONCLUSIVE (no hard failure, but evidence is too weak to endorse)
    - one or more "extreme" interior scores (< EPS_EXTREME or > 1-EPS_EXTREME)
    - a normalized weight above MAX_NORMALIZED_WEIGHT
    - an arm ESS fraction below ESS_FRACTION_WARN

ACCEPT otherwise.

Only aggregate, non-identifying statistics are ever logged or returned; raw
rows/covariates never leave the kernel (sensitive-data redaction).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from .contract import (
    ArmEvidence,
    Decision,
    DiagnosticResult,
    EPS_EXTREME,
    IPWConfig,
    ObservationSet,
    RejectReason,
)
from .balance import BALANCE_BASIS, max_balance_z
from .weights import effective_sample_size

logger = logging.getLogger("ipw_ate.diagnostics")

# Declared inconclusive thresholds.
MAX_NORMALIZED_WEIGHT: float = 10.0
ESS_FRACTION_WARN: float = 0.5
#: A point is "beyond support" if it exceeds the opposite arm's support edge
#: by more than this many *standardized* units on a covariate.
SUPPORT_GAP_SD: float = 0.5
#: A positivity void needs at least this many units (scales mildly with n), so
#: isolated tail points do not by themselves trigger a hard rejection.
SUPPORT_VOID_MIN_FRAC: float = 0.02
SUPPORT_VOID_MIN_COUNT: int = 6
#: Beyond-support units count as a void only when they form a *tight cluster*
#: (span <= this, in standardized units). A sparse heavy tail is spread over a
#: wide range and is left to the weight/extreme-score rules instead of being
#: mistaken for a structural positivity failure.
SUPPORT_VOID_MAX_SPAN: float = 0.75
#: Weighted covariate-balance z thresholds over the augmented basis. Calibrated
#: empirically: a correct model stays below ~3 across sample sizes, while a
#: misspecified linear propensity reaches 7+ even at moderate n.
BALANCE_Z_INCONCLUSIVE: float = 3.0
BALANCE_Z_REJECT: float = 4.0

ASSUMPTIONS: tuple[str, ...] = (
    "unconfoundedness: Y(0),Y(1) independent of T given X",
    "positivity: 0 < P(T=1|X) < 1 for all units in the target population",
    "SUTVA: no interference and a single well-defined treatment version",
    "correctly specified or sufficiently flexible propensity model",
)


@dataclass(frozen=True)
class _ArmStats:
    n: int
    weight_sum: float
    ess: float
    max_weight: float
    mean_ps: float


def _arm_stat(
    mask: np.ndarray, weights: np.ndarray, propensity: np.ndarray
) -> _ArmStats:
    w = weights[mask]
    return _ArmStats(
        n=int(mask.sum()),
        weight_sum=float(w.sum()) if w.size else 0.0,
        ess=effective_sample_size(w),
        max_weight=float(w.max()) if w.size else 0.0,
        mean_ps=float(propensity[mask].mean()) if w.size else float("nan"),
    )


def _standardized(data: ObservationSet) -> np.ndarray:
    mean = data.covariates.mean(axis=0)
    sd = data.covariates.std(axis=0, ddof=0)
    sd = np.where(sd < 1e-12, 1.0, sd)
    return (data.covariates - mean) / sd


def count_support_voids(data: ObservationSet) -> int:
    """Count single-arm *support voids* on the covariate marginals.

    A void is a direction (treated high/low or control high/low on one
    covariate) in which a cluster of at least ``min_cluster`` units lies more
    than :data:`SUPPORT_GAP_SD` standardized units beyond the opposite arm's
    observed support edge. Unlike quantile bins, this inspects the value
    range, so a genuine single-arm region at the edge is detected while
    well-overlapped data yields zero.
    """
    if data.p == 0:
        return 0
    z = _standardized(data)
    treated = data.treatment == 1
    control = ~treated
    n = data.n
    min_cluster = max(
        SUPPORT_VOID_MIN_COUNT, int(round(SUPPORT_VOID_MIN_FRAC * n))
    )

    voids = 0
    for j in range(data.p):
        zt, zc = z[treated, j], z[control, j]
        if zt.size == 0 or zc.size == 0:
            continue
        # Each of the four directional exceedances is a candidate void. A
        # sparse heavy tail spans a wide range; a genuine single-arm region is
        # a tight cluster, hence the span cap.
        candidates = (
            (zt[zt > zc.max() + SUPPORT_GAP_SD]),
            (zt[zt < zc.min() - SUPPORT_GAP_SD]),
            (zc[zc > zt.max() + SUPPORT_GAP_SD]),
            (zc[zc < zt.min() - SUPPORT_GAP_SD]),
        )
        for sel in candidates:
            if sel.size >= min_cluster and float(sel.max() - sel.min()) <= (
                SUPPORT_VOID_MAX_SPAN
            ):
                voids += 1
    return voids


def count_single_arm_cells(data: ObservationSet) -> int:
    """Backward-compatible alias for the positivity-void screen."""
    return count_support_voids(data)


def build_diagnostic(
    data: ObservationSet,
    weights: np.ndarray,
    propensity: np.ndarray,
    fold_ids: np.ndarray,
    folds: tuple[tuple[int, ...], ...],
    config: IPWConfig,
    request_id: str,
) -> DiagnosticResult:
    """Compute the evidence packet and derive the decision."""
    treated = data.treatment == 1
    control = ~treated
    s1, s0 = _arm_stat(treated, weights, propensity), _arm_stat(
        control, weights, propensity
    )

    n_boundary = int(
        np.sum((propensity <= 0.0) | (propensity >= 1.0) | ~np.isfinite(propensity))
    )
    n_extreme = int(
        np.sum(
            ((propensity < EPS_EXTREME) | (propensity > 1.0 - EPS_EXTREME))
            & (propensity > 0.0)
            & (propensity < 1.0)
        )
    )
    single_cells = count_single_arm_cells(data)
    balance_z = max_balance_z(data.treatment, weights, data.covariates)

    reject: list[str] = []
    inconclusive: list[str] = []

    if s1.n == 0 or s0.n == 0:
        reject.append(RejectReason.EMPTY_ARM.value)
    if n_boundary > 0:
        reject.append(RejectReason.SCORE_AT_BOUNDARY.value)
    if single_cells > 0:
        reject.append(RejectReason.NO_OVERLAP_CELL.value)
    if min(s1.ess, s0.ess) < config.min_ess_per_arm:
        reject.append(RejectReason.LOW_ESS.value)
    if balance_z > BALANCE_Z_REJECT:
        reject.append(RejectReason.COVARIATE_IMBALANCE.value)

    if n_extreme > 0:
        inconclusive.append(RejectReason.EXTREME_WEIGHTS.value)
    max_w = max(s1.max_weight, s0.max_weight)
    if max_w > MAX_NORMALIZED_WEIGHT:
        inconclusive.append(RejectReason.EXTREME_WEIGHTS.value)
    for st in (s1, s0):
        if st.n > 0 and st.ess / st.n < ESS_FRACTION_WARN:
            inconclusive.append(RejectReason.LOW_ESS.value)
            break
    if BALANCE_Z_INCONCLUSIVE < balance_z <= BALANCE_Z_REJECT:
        inconclusive.append(RejectReason.COVARIATE_IMBALANCE.value)

    reasons = tuple(dict.fromkeys(reject + inconclusive))
    if reject:
        decision = Decision.REJECT
    elif inconclusive:
        decision = Decision.INCONCLUSIVE
    else:
        decision = Decision.ACCEPT

    arms = (
        ArmEvidence(1, s1.n, s1.weight_sum, s1.ess, s1.max_weight, s1.mean_ps),
        ArmEvidence(0, s0.n, s0.weight_sum, s0.ess, s0.max_weight, s0.mean_ps),
    )
    message = _message(decision, reasons, s1, s0, single_cells, balance_z)

    result = DiagnosticResult(
        decision=decision,
        reasons=reasons,
        request_id=request_id,
        n=data.n,
        n_treated=s1.n,
        n_control=s0.n,
        n_splits=config.n_splits,
        estimand=config.estimand.value,
        trim_version=config.trim_version,
        score_min=float(propensity.min()),
        score_max=float(propensity.max()),
        n_extreme_scores=n_extreme,
        n_boundary_scores=n_boundary,
        prop_std=float(propensity.std(ddof=0)),
        max_weight=max_w,
        ess_treated=s1.ess,
        ess_control=s0.ess,
        single_arm_cells=single_cells,
        max_balance_z=balance_z,
        balance_basis=BALANCE_BASIS,
        folds=folds,
        arms=arms,
        assumptions=ASSUMPTIONS,
        message=message,
    )
    _log_redacted(result)
    return result


def _message(
    decision: Decision,
    reasons: tuple[str, ...],
    s1: _ArmStats,
    s0: _ArmStats,
    single_cells: int,
    balance_z: float,
) -> str:
    if decision is Decision.ACCEPT:
        return (
            "accept: interior scores, both arms present in populated cells, "
            f"weighted balance z={balance_z:.2f}, "
            f"ESS treated={s1.ess:.1f} control={s0.ess:.1f}"
        )
    if decision is Decision.REJECT:
        return (
            "reject: hard overlap/weight/model failure -> "
            + ", ".join(reasons)
            + f"; single-arm cells={single_cells}, "
            f"max balance z={balance_z:.2f}, "
            f"ESS treated={s1.ess:.1f} control={s0.ess:.1f}"
        )
    return (
        "inconclusive: no hard failure but weak overlap/model evidence -> "
        + ", ".join(reasons)
        + f"; max balance z={balance_z:.2f}, "
        f"ESS treated={s1.ess:.1f} ({s1.ess / max(s1.n, 1):.2f}/n) "
        f"control={s0.ess:.1f} ({s0.ess / max(s0.n, 1):.2f}/n)"
    )


def _log_redacted(d: DiagnosticResult) -> None:
    """Emit only aggregates + request id; never row-level covariate/outcome."""
    payload = {
        "request_id": d.request_id,
        "decision": d.decision.value,
        "reasons": list(d.reasons),
        "n": d.n,
        "n_treated": d.n_treated,
        "n_control": d.n_control,
        "ess_treated": round(d.ess_treated, 3),
        "ess_control": round(d.ess_control, 3),
        "score_min": round(d.score_min, 6),
        "score_max": round(d.score_max, 6),
        "n_extreme_scores": d.n_extreme_scores,
        "single_arm_cells": d.single_arm_cells,
        "max_balance_z": round(d.max_balance_z, 3),
        "max_weight": round(d.max_weight, 3),
    }
    if d.decision is Decision.REJECT:
        logger.warning("overlap diagnostic %s", payload)
    else:
        logger.info("overlap diagnostic %s", payload)
