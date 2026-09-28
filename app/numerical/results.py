"""Result structures and the accept/reject decision construction.

This module turns the measured iterates and per-column error histories of the
refinement engine into the serializable evidence envelope. It owns the rules
that distinguish *accepted*, *not met* and *singular* outcomes, including the
residual-precision floor and the cond(A)*eta forward-accuracy explanation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from mpmath import mpf

from ..config import Config
from ..logging_setup import get_logger
from . import mp, workprec
from .condition import ConditionReport

LOG = get_logger("reporting")

# Solve outcomes.
ACCEPTED = "accepted"
NOT_MET = "not_met"
SINGULAR = "singular_inconclusive"
SKIPPED_SINGULAR = "skipped_numerically_singular"


@dataclass(frozen=True)
class StageEvent:
    stage: str
    iteration: int
    per_column_eta: list[str]
    note: str


@dataclass(frozen=True)
class StageRecord:
    stage: str
    used: bool
    factor_pivot_ratio: str
    iterations: int
    note: str


@dataclass(frozen=True)
class ColumnOutcome:
    column: int
    status: str
    best_eta: str
    final_omega: str
    forward_bound: str
    reason: str
    accepted_at_stage: str | None
    accepted_at_iteration: int | None


@dataclass(frozen=True)
class SolveResult:
    request_id: str
    status: str
    reason: str
    n: int
    nrhs: int
    condition: dict
    precision_floor_eta: str
    columns: list[ColumnOutcome]
    stages: list[StageRecord]
    events: list[StageEvent]
    solution: list[list[str]] | None
    elapsed_seconds: float

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "reason": self.reason,
            "shape": {"n": self.n, "nrhs": self.nrhs},
            "condition": self.condition,
            "precision_floor_eta": self.precision_floor_eta,
            "columns": [vars(c) for c in self.columns],
            "stages": [vars(s) for s in self.stages],
            "events": [vars(e) for e in self.events],
            "solution": self.solution,
            "elapsed_seconds": round(self.elapsed_seconds, 6),
        }


def condition_payload(report: ConditionReport, residual_dps: int) -> dict:
    log10 = report.log10_cond
    return {
        "method": report.method,
        "cond": mp.nstr(report.cond, 8),
        "log10_cond": None if log10 == float("inf") else round(log10, 3),
        "svd_cond": None if report.svd_cond is None else mp.nstr(report.svd_cond, 8),
        "estimated_at_dps": report.cond_dps,
        "digits_lost_estimate": None if log10 == float("inf") else round(log10, 2),
        "attainable_note": (
            f"Residual evaluated at {residual_dps} dps: normwise backward error "
            f"floor ~ 1e{1 - residual_dps}. Relative forward accuracy is bounded "
            f"by cond(A)*eta (log10 cond ~ {_fmt_log10(log10)}), so a float64 "
            f"direct solve is limited to roughly {max(0.0, 16 - log10):.1f} "
            "correct digits without refinement."
        ),
    }


def singular_result(
    *,
    request_id: str,
    n: int,
    nrhs: int,
    evidence: dict,
    started: float,
    config: Config,
) -> SolveResult:
    columns = [
        ColumnOutcome(
            column=j,
            status=SKIPPED_SINGULAR,
            best_eta="n/a",
            final_omega="n/a",
            forward_bound="n/a",
            reason=f"no solution attempted: {evidence['summary']}",
            accepted_at_stage=None,
            accepted_at_iteration=None,
        )
        for j in range(nrhs)
    ]
    condition = {
        "method": "high-precision SVD",
        "log10_cond": None,
        "cond": "inf",
        "svd_cond": evidence["svd_cond"],
        "estimated_at_dps": config.rank_probe_dps,
        "digits_lost_estimate": None,
        "attainable_note": (
            "Singular system: no unique solution; least-squares / minimum-norm "
            "treatment is outside this service's contract."
        ),
    }
    return SolveResult(
        request_id=request_id,
        status=SINGULAR,
        reason=evidence["summary"],
        n=n,
        nrhs=nrhs,
        condition=condition,
        precision_floor_eta=mp.nstr(mpf(10) ** (1 - config.residual_dps), 6),
        columns=columns,
        stages=[
            StageRecord(
                stage=f"mp-{config.rank_probe_dps}-rank-probe",
                used=False,
                factor_pivot_ratio=evidence["pivot_ratio"],
                iterations=0,
                note=evidence["summary"],
            )
        ],
        events=[],
        solution=None,
        elapsed_seconds=time.perf_counter() - started,
    )


def no_usable_factorization(
    *,
    request_id: str,
    n: int,
    nrhs: int,
    cond_report: ConditionReport,
    floor: mpf,
    stage_records: list[StageRecord],
    started: float,
    config: Config,
) -> SolveResult:
    columns = [
        ColumnOutcome(
            column=j,
            status=NOT_MET,
            best_eta="unmeasured",
            final_omega="unmeasured",
            forward_bound="unmeasured",
            reason=(
                "NOT MET: every configured factorization stage was judged "
                "numerically singular and no iterate could be formed; widen the "
                "precision ladder. No convergence reported."
            ),
            accepted_at_stage=None,
            accepted_at_iteration=None,
        )
        for j in range(nrhs)
    ]
    return SolveResult(
        request_id=request_id,
        status=NOT_MET,
        reason="no usable factorization stage in the configured ladder",
        n=n,
        nrhs=nrhs,
        condition=condition_payload(cond_report, config.residual_dps),
        precision_floor_eta=mp.nstr(floor, 6),
        columns=columns,
        stages=stage_records,
        events=[],
        solution=None,
        elapsed_seconds=time.perf_counter() - started,
    )


def finalize(
    *,
    request_id: str,
    a,
    b,
    x,
    config: Config,
    cond_report: ConditionReport,
    floor: mpf,
    measured_reports,
    done: list[bool],
    accepted_stage: list[str | None],
    accepted_iter: list[int | None],
    stage_records: list[StageRecord],
    events: list[StageEvent],
    started: float,
) -> SolveResult:
    """Decide every column against its measured residual and build the result."""
    n, nrhs = a.rows, b.cols
    tol = mpf(config.backward_tol)
    forward_tol = mpf(config.forward_tol)
    cond = cond_report.cond
    floor_neighborhood = floor * mpf(100)

    outcomes = [
        _decide_column(
            j=j,
            report=measured_reports[j],
            cond=cond,
            tol=tol,
            forward_tol=forward_tol,
            floor_band=floor_neighborhood,
            accepted=done[j],
            stage=accepted_stage[j],
            iteration=accepted_iter[j],
            config=config,
        )
        for j in range(nrhs)
    ]

    overall, overall_reason = _overall_summary(outcomes, nrhs, config.backward_tol)
    solution = _serialize_solution(x, n, nrhs, config.solution_output_dps)
    if overall == "accepted":
        LOG.info("solve end status=accepted accepted=%d/%d", sum(done), nrhs)
    else:
        n_accepted = sum(c.status == ACCEPTED for c in outcomes)
        LOG.warning("solve end status=%s accepted=%d/%d", overall, n_accepted, nrhs)

    return SolveResult(
        request_id=request_id,
        status=overall,
        reason=overall_reason,
        n=n,
        nrhs=nrhs,
        condition=condition_payload(cond_report, config.residual_dps),
        precision_floor_eta=mp.nstr(floor, 6),
        columns=outcomes,
        stages=stage_records,
        events=events,
        solution=solution,
        elapsed_seconds=time.perf_counter() - started,
    )


def _decide_column(
    *,
    j: int,
    report,
    cond: mpf,
    tol: mpf,
    forward_tol: mpf,
    floor_band: mpf,
    accepted: bool,
    stage: str | None,
    iteration: int | None,
    config: Config,
) -> ColumnOutcome:
    eta = report.eta_normwise
    omega = report.omega_componentwise
    bound = _forward_bound(cond, eta)
    if accepted and eta <= tol * mpf(10):
        return ColumnOutcome(
            column=j,
            status=ACCEPTED,
            best_eta=mp.nstr(eta, 8),
            final_omega=mp.nstr(omega, 6),
            forward_bound=mp.nstr(bound, 4),
            reason=_accepted_reason(eta, bound, forward_tol, stage, iteration, config),
            accepted_at_stage=stage,
            accepted_at_iteration=iteration,
        )
    return ColumnOutcome(
        column=j,
        status=NOT_MET,
        best_eta=mp.nstr(eta, 8),
        final_omega=mp.nstr(omega, 6),
        forward_bound=mp.nstr(bound, 4),
        reason=_rejected_reason(eta, bound, tol, floor_band, config),
        accepted_at_stage=None,
        accepted_at_iteration=None,
    )


def _accepted_reason(eta, bound, forward_tol, stage, iteration, config: Config) -> str:
    reason = (
        f"backward error {mp.nstr(eta, 4)} <= tol {config.backward_tol} "
        f"at stage {stage} iteration {iteration}; residual formed with original "
        f"A at {config.residual_dps} dps"
    )
    if bound != mpf("inf") and bound > forward_tol:
        reason += (
            f"; forward bound ~{mp.nstr(bound, 3)} exceeds forward_tol "
            f"{config.forward_tol}: accuracy limited by cond(A)"
        )
    return reason


def _rejected_reason(eta, bound, tol, floor_band, config: Config) -> str:
    if eta <= floor_band:
        return (
            f"NOT MET: backward error {mp.nstr(eta, 4)} is at the residual "
            f"precision floor ~{mp.nstr(floor_band, 3)} ({config.residual_dps} "
            "dps); raise residual_dps to go tighter. No convergence reported."
        )
    if bound != mpf("inf") and bound > mpf(1) / mpf(2):
        return (
            f"NOT MET: cond(A)*eta ~ {mp.nstr(bound, 3)} >= 1/2, so component "
            "errors cannot be bounded reliably; the system is at the limit of "
            "attainable accuracy. No convergence reported."
        )
    return (
        f"NOT MET: best measured backward error {mp.nstr(eta, 4)} > tol "
        f"{config.backward_tol} after exhausting the precision ladder; "
        "stagnation triggered escalation and tolerance was never verified. "
        "No convergence reported."
    )


def _overall_summary(outcomes: list[ColumnOutcome], nrhs: int, tol: str):
    n_accepted = sum(c.status == ACCEPTED for c in outcomes)
    if n_accepted == nrhs:
        return "accepted", f"all {nrhs} column(s) meet backward tolerance {tol}"
    if n_accepted == 0:
        return "not_met", f"0/{nrhs} columns meet backward tolerance; see per-column reasons"
    return (
        "partially_accepted",
        f"{n_accepted}/{nrhs} columns meet backward tolerance; each column reported independently",
    )


def _serialize_solution(x, n: int, nrhs: int, output_dps: int):
    with workprec(output_dps):
        return [
            [mp.nstr(x[i, j], output_dps) for j in range(nrhs)]
            for i in range(n)
        ]


def _forward_bound(cond: mpf, eta: mpf) -> mpf:
    if cond == mpf("inf") or eta == mpf("inf"):
        return mpf("inf")
    return cond * eta


def _fmt_log10(value: float) -> str:
    return "inf" if value == float("inf") else f"{value:.2f}"
