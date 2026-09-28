"""The mixed-precision iterative-refinement driver.

Precision contract
------------------
* The original matrix A and right-hand sides B are held as mpmath matrices and
  never rounded for residual formation.
* The residual ``r = b - A x`` is evaluated at the fixed residual precision
  ``config.residual_dps`` (default 60 decimal digits). This places a floor of
  roughly ``10**(1-residual_dps)`` on the attainable normwise backward error;
  that floor is reported when a tighter target is requested.
* Factorizations ascend a ladder: float32 -> float64 -> mp-30 -> mp-60 ->
  mp-100 (selectable). Each correction is solved at that stage's own
  precision and accumulated into the high-precision iterate.
* When corrections stop improving the residual for a column, that column is
  marked stagnant and the engine escalates precision. If the ladder is
  exhausted, :mod:`results` reports NOT MET -- convergence is never claimed
  without a measured residual meeting the tolerance, and the returned iterate
  is always one whose residual was actually measured.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from mpmath import mpf

from ..config import Config
from ..logging_setup import get_logger
from . import mp, mp_epsilon, workprec
from . import arithmetic as arith
from . import error_metrics
from . import results
from .condition import estimate_condition, svd_condition
from .factorization import BinaryLU, MPLU, factor_binary, factor_mp
from .results import (
    SolveResult,
    StageEvent,
    StageRecord,
)

LOG = get_logger("engine")


@dataclass(frozen=True)
class StageSpec:
    kind: str  # "float32" | "float64" | "mp"
    dps: int | None = None

    @property
    def label(self) -> str:
        return self.kind if self.dps is None else f"mp-{self.dps}"


def build_ladder(config: Config) -> list[StageSpec]:
    stages: list[StageSpec] = []
    if config.use_fp32_first:
        stages.append(StageSpec("float32"))
    if config.use_fp64:
        stages.append(StageSpec("float64"))
    stages.extend(StageSpec("mp", dps=d) for d in config.mp_dps_ladder)
    return stages


def solve_system(a, b, config: Config, request_id: str) -> SolveResult:
    started = time.perf_counter()
    n, nrhs = a.rows, b.cols
    tol = mpf(config.backward_tol)
    floor = mp.mpf(10) ** (1 - config.residual_dps)
    LOG.info("solve start n=%d nrhs=%d tol=%s", n, nrhs, config.backward_tol)

    probe = factor_mp(a, config.rank_probe_dps, mpf(config.singular_pivot_factor))
    evidence = _singular_evidence(a, probe, config)
    if probe.singular:
        LOG.warning("matrix singular/inconclusive %s", evidence["summary"])
        return results.singular_result(
            request_id=request_id, n=n, nrhs=nrhs, evidence=evidence,
            started=started, config=config,
        )

    cond_report = estimate_condition(a, probe, config.rank_probe_dps, config.svd_max_n)
    LOG.info("condition estimate log10_cond=%.2f method=%s",
             cond_report.log10_cond, cond_report.method)

    state = RefinementState(nrhs)
    x, stage_records, events = _run_ladder(
        a, b, config, tol, cond_report, state, started, floor, request_id
    )
    if x is None:
        return results.no_usable_factorization(
            request_id=request_id, n=n, nrhs=nrhs, cond_report=cond_report,
            floor=floor, stage_records=stage_records, started=started, config=config,
        )

    _, measured_reports = _residual_and_errors(a, x, b, config)
    return results.finalize(
        request_id=request_id, a=a, b=b, x=x, config=config,
        cond_report=cond_report, floor=floor, measured_reports=measured_reports,
        done=state.done, accepted_stage=state.accepted_stage,
        accepted_iter=state.accepted_iter, stage_records=stage_records,
        events=events, started=started,
    )


class RefinementState:
    """Per-column progress shared across precision stages."""

    def __init__(self, nrhs: int) -> None:
        self.nrhs = nrhs
        self.best_eta = [mpf("inf")] * nrhs
        self.best_omega = [mpf("inf")] * nrhs
        self.done = [False] * nrhs
        self.accepted_stage: list[str | None] = [None] * nrhs
        self.accepted_iter: list[int | None] = [None] * nrhs


def _run_ladder(a, b, config, tol, cond_report, state, started, floor, request_id):
    """Ascend the precision ladder until every column is done or it is spent."""
    x = None
    stage_records: list[StageRecord] = []
    events: list[StageEvent] = []
    for spec in build_ladder(config):
        if all(state.done):
            break
        handle, record = _factor_stage(a, spec, config)
        stage_records.append(record)
        if handle is None:
            LOG.info("stage %s skipped: %s", spec.label, record.note)
            continue
        if x is None:
            x = _initial_solution(handle, b, config)
        x, ev, iters = _run_one_stage(
            handle, spec, a, b, x, config, tol, state
        )
        events.extend(ev)
        stage_records[-1] = StageRecord(
            stage=record.stage, used=True,
            factor_pivot_ratio=record.factor_pivot_ratio, iterations=iters,
            note=(f"{iters} refinement iteration(s); "
                  f"{sum(state.done)}/{state.nrhs} column(s) within tolerance overall"),
        )
        LOG.info("stage %s complete accepted_columns=%d/%d",
                 spec.label, sum(state.done), state.nrhs)
    return x, stage_records, events


def _run_one_stage(handle, spec, a, b, x, config, tol, state):
    x, events, iters = _refine_stage(
        handle=handle, spec=spec, a=a, b=b, x=x, config=config,
        tol=tol, state=state,
    )
    return x, events, iters


# --------------------------------------------------------------------------- #
# Singularity evidence
# --------------------------------------------------------------------------- #


def _singular_evidence(a, probe: MPLU, config: Config) -> dict:
    """Two independent pieces of evidence: LU pivots and high-dps SVD."""
    n = probe.n
    with workprec(config.rank_probe_dps):
        scale = max(abs(probe.lu[k][k]) for k in range(n)) or mpf(1)
        threshold = mp_epsilon(config.rank_probe_dps) * mpf(
            config.singular_pivot_factor
        )
        rank = sum(
            1 for k in range(n) if abs(probe.lu[k][k]) >= scale * threshold
        )
        s_min = "not_estimated(n>svd_max_n)"
        svd = None
        if n <= config.svd_max_n:
            svd = svd_condition(a, config.rank_probe_dps)
            try:
                svals = sorted(abs(s) for s in mp.svd(a, compute_uv=False))
                s_min = mp.nstr(svals[0], 8)
            except Exception:
                s_min = "unavailable"
    exact_zero = any(probe.lu[k][k] == 0 for k in range(n))
    kind = (
        "exact zero pivot in high-precision LU"
        if exact_zero
        else "pivot ratio below high-precision rank threshold"
    )
    summary = f"rank {rank}/{n}: {kind}; smallest singular value ~ {s_min}"
    return {
        "rank": rank,
        "expected_rank": n,
        "min_singular_value": s_min,
        "pivot_ratio": mp.nstr(probe.pivot_ratio, 6),
        "svd_cond": None if svd is None else mp.nstr(svd, 8),
        "summary": summary,
    }


# --------------------------------------------------------------------------- #
# Stage factorization and initial solves
# --------------------------------------------------------------------------- #


def _factor_stage(a, spec: StageSpec, config: Config):
    pivot_factor = mpf(config.singular_pivot_factor)
    if spec.kind in ("float32", "float64"):
        handle = factor_binary(a, spec.kind)
        if handle.singular:
            return None, StageRecord(
                stage=spec.label,
                used=False,
                factor_pivot_ratio=mp.nstr(mpf(handle.pivot_ratio), 4),
                iterations=0,
                note=(
                    f"factorization numerically singular at {spec.label} "
                    f"(pivot ratio {handle.pivot_ratio:.3e}); escalating precision"
                ),
            )
        return handle, StageRecord(
            stage=spec.label,
            used=True,
            factor_pivot_ratio=mp.nstr(mpf(handle.pivot_ratio), 4),
            iterations=0,
            note="",
        )

    handle = factor_mp(a, spec.dps, pivot_factor)
    if handle.singular:
        return None, StageRecord(
            stage=spec.label,
            used=False,
            factor_pivot_ratio=mp.nstr(handle.pivot_ratio, 4),
            iterations=0,
            note=f"numerically singular at {spec.label}; escalating",
        )
    return handle, StageRecord(
        stage=spec.label,
        used=True,
        factor_pivot_ratio=mp.nstr(handle.pivot_ratio, 4),
        iterations=0,
        note="",
    )


def _initial_solution(handle, b, config: Config):
    n, nrhs = b.rows, b.cols
    with workprec(config.residual_dps):
        x = arith.make_matrix(n, nrhs)
        for j in range(nrhs):
            col = [b[i, j] for i in range(n)]
            d = _solve_at_stage(handle, col)
            for i in range(n):
                x[i, j] = d[i]
    return x


def _solve_at_stage(handle, column: list[mpf]) -> list[mpf]:
    if isinstance(handle, BinaryLU):
        import numpy as np

        rhs = np.array([float(v) for v in column], dtype=handle.dtype)
        out = handle.solve_columns(rhs)
        return [mpf(float(v)) for v in out]
    with workprec(handle.dps):
        return [mpf(v) for v in handle.solve_column(column)]


# --------------------------------------------------------------------------- #
# Iterative refinement within one stage
# --------------------------------------------------------------------------- #


def _residual_and_errors(a, x, b, config: Config):
    with workprec(config.residual_dps):
        r = arith.residual_matrix(a, x, b)
        reports = error_metrics.column_errors(a, x, b, r)
    return r, reports


def _refine_stage(*, handle, spec, a, b, x, config, tol, state):
    stagnation = mpf(config.stagnation_factor)
    max_iters = config.max_iterations_per_stage
    nrhs, n_rows = b.cols, a.rows
    events: list[StageEvent] = []
    prev_eta = list(state.best_eta)
    stale_count = [0] * nrhs
    stage_iters = 0

    for iteration in range(1, max_iters + 1):
        stage_iters = iteration
        r, reports = _residual_and_errors(a, x, b, config)
        eta_now = [rep.eta_normwise for rep in reports]
        active = [j for j in range(nrhs) if not state.done[j]]
        if not active:
            break

        events.append(
            StageEvent(
                stage=spec.label,
                iteration=iteration,
                per_column_eta=[mp.nstr(eta_now[j], 4) for j in range(nrhs)],
                note=_iteration_note(state.done, stale_count, nrhs),
            )
        )

        newly_done = _update_progress(
            active, eta_now, reports, tol, stagnation, prev_eta,
            stale_count, state, spec, iteration,
        )
        if newly_done:
            LOG.info(
                "stage %s iter %d accepted columns=%s",
                spec.label, iteration, newly_done,
            )

        active = [j for j in range(nrhs) if not state.done[j]]
        if not active:
            break
        if all(stale_count[j] >= 2 for j in active):
            LOG.info(
                "stage %s stagnated for columns=%s after %d iterations",
                spec.label, active, iteration,
            )
            break
        if iteration >= max_iters:
            # Do not apply a correction the budget cannot re-measure.
            LOG.info(
                "stage %s reached iteration budget %d for columns=%s",
                spec.label, max_iters, active,
            )
            break

        _apply_corrections(handle, a, r, x, active, n_rows, config)

    # state is updated in place for per-column progress.
    return x, events, stage_iters


def _update_progress(active, eta_now, reports, tol, stagnation, prev_eta,
                     stale_count, state, spec, iteration):
    newly_done: list[int] = []
    for j in active:
        if eta_now[j] <= tol:
            state.done[j] = True
            state.accepted_stage[j] = spec.label
            state.accepted_iter[j] = iteration
            state.best_eta[j] = eta_now[j]
            state.best_omega[j] = reports[j].omega_componentwise
            newly_done.append(j)
            continue
        if eta_now[j] < state.best_eta[j]:
            state.best_eta[j] = eta_now[j]
        state.best_omega[j] = reports[j].omega_componentwise
        if eta_now[j] >= stagnation * prev_eta[j]:
            stale_count[j] += 1
        else:
            stale_count[j] = 0
        prev_eta[j] = eta_now[j]
    return newly_done


def _apply_corrections(handle, a, r, x, active, n_rows, config):
    # Solve A d_j = r_j at THIS stage's precision and accumulate. The new
    # iterate is measured at the top of the next loop iteration.
    with workprec(config.residual_dps):
        for j in active:
            rcol = [r[i, j] for i in range(n_rows)]
            delta = _solve_at_stage(handle, rcol)
            for i in range(n_rows):
                x[i, j] = x[i, j] + delta[i]


def _iteration_note(done, stale_count, nrhs) -> str:
    accepted = [j for j in range(nrhs) if done[j]]
    stale = [j for j in range(nrhs) if not done[j] and stale_count[j] >= 2]
    parts = []
    if accepted:
        parts.append(f"accepted={accepted}")
    if stale:
        parts.append(f"stagnant={stale}")
    return "; ".join(parts) or "refining"
