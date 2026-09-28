"""Service layer: orchestration, run registry, status decisions.

The service owns the cross-layer contract:
- validation errors propagate as INPUT_INVALID / RESOURCE_EXHAUSTED
- a run_id replayed with the same payload returns the stored report
  (idempotent replay); replayed with a different payload -> STATE_CONFLICT
- iteration exhaustion is NOT an error: the report is PARTIAL and carries
  the unconverged roots with converged=False
- kernel failures propagate as COMPUTATION_FAILED
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import uuid
from typing import Dict, Optional, Sequence, Tuple

import numpy as np

from . import evidence as evidence_mod
from . import ordering as ordering_mod
from .domain import (
    HARD_MAX_ITER,
    Method,
    RootEstimate,
    SolveOptions,
    SolveReport,
    SolveStatus,
)
from .errors import ResourceExhaustedError, StateConflictError
from .kernel import aberth_refine, cauchy_initial_guesses, companion_roots
from .runlog import NullLogger
from .validation import normalize, parse_coefficients


class RunRegistry:
    """In-memory record of completed runs, keyed by run_id.

    Supports replay (same run_id + same payload -> stored report) and
    conflict detection (same run_id + different payload -> STATE_CONFLICT).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: Dict[str, Tuple[str, dict]] = {}

    @staticmethod
    def fingerprint(coefficients: Sequence, options: SolveOptions) -> str:
        canonical = json.dumps(
            {
                "coefficients": [[float(p[0]), float(p[1])] for p in coefficients],
                "options": {
                    "method": options.method.value,
                    "max_iter": options.max_iter,
                    "conv_tol": options.conv_tol,
                    "cluster_tol": options.cluster_tol,
                    "pair_tol": options.pair_tol,
                    "max_degree": options.max_degree,
                },
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def check_replay(self, run_id: str, fingerprint: str) -> Optional[dict]:
        with self._lock:
            entry = self._runs.get(run_id)
        if entry is None:
            return None
        stored_fp, report = entry
        if stored_fp != fingerprint:
            raise StateConflictError(
                "run_id already exists with a different request payload; "
                "choose a new run_id or replay the original request",
                detail={"run_id": run_id},
                run_id=run_id,
            )
        return report

    def store(self, run_id: str, fingerprint: str, report: dict) -> None:
        with self._lock:
            self._runs[run_id] = (fingerprint, report)

    def get(self, run_id: str) -> Optional[dict]:
        with self._lock:
            entry = self._runs.get(run_id)
        return entry[1] if entry else None


class SolverService:
    def __init__(self, registry: Optional[RunRegistry] = None, logger=None) -> None:
        self.registry = registry or RunRegistry()
        self.logger = logger if logger is not None else NullLogger()

    def solve(
        self,
        coefficients: Sequence[Sequence[float]],
        options: Optional[SolveOptions] = None,
        run_id: Optional[str] = None,
    ) -> dict:
        options = options or SolveOptions()
        run_id = run_id or uuid.uuid4().hex[:16]

        if options.max_iter > HARD_MAX_ITER:
            raise ResourceExhaustedError(
                f"max_iter {options.max_iter} exceeds the hard cap of {HARD_MAX_ITER}",
                detail={"max_iter": options.max_iter, "hard_cap": HARD_MAX_ITER},
                run_id=run_id,
            )

        fingerprint = RunRegistry.fingerprint(coefficients, options)
        replay = self.registry.check_replay(run_id, fingerprint)
        if replay is not None:
            self.logger.log(run_id, "replay", reason="identical payload already solved")
            return replay

        self.logger.log(run_id, "request", n_coefficients=len(coefficients),
                        method=options.method.value, max_iter=options.max_iter)

        try:
            report = self._solve_fresh(coefficients, options, run_id)
        except Exception as exc:
            category = getattr(exc, "category", None)
            self.logger.log(
                run_id, "decision",
                status="failed",
                reason=str(exc),
                category=category.value if category else "unexpected",
            )
            raise

        self.registry.store(run_id, fingerprint, report)
        return report

    def _solve_fresh(self, coefficients, options: SolveOptions, run_id: str) -> dict:
        raw = parse_coefficients(coefficients, run_id=run_id)
        poly = normalize(raw, max_degree=options.max_degree, run_id=run_id)
        self.logger.log(
            run_id, "normalized",
            degree=poly.degree,
            leading_dropped=poly.leading_dropped,
            scale=poly.scale,
        )

        if options.method is Method.ABERTH:
            initial = cauchy_initial_guesses(poly)
        else:
            initial = companion_roots(poly, run_id=run_id)
            self.logger.log(run_id, "companion_done", n_roots=len(initial))

        if options.method is Method.COMPANION:
            # No iterative stage: companion eigenvalues are the final state.
            roots = initial
            converged = np.ones(poly.degree, dtype=bool)
            iterations = np.zeros(poly.degree, dtype=int)
            steps_taken = 0
        else:
            result = aberth_refine(
                poly, initial,
                max_iter=options.max_iter,
                conv_tol=options.conv_tol,
                run_id=run_id,
            )
            roots = result.roots
            converged = result.converged
            iterations = result.iterations
            steps_taken = result.steps_taken
            self.logger.log(
                run_id, "aberth_done",
                sweeps=steps_taken,
                converged=int(np.sum(converged)),
                total=poly.degree,
                max_step_tail=result.max_step_history[-3:],
            )

        ev, res_abs, res_rel, error_bound = evidence_mod.build_evidence(
            poly, roots, cluster_tol=options.cluster_tol
        )
        order = ordering_mod.order_indices(roots)
        pairs, unpaired = ordering_mod.conjugate_pairs(roots, options.pair_tol, order)

        # Cluster membership was computed in kernel order; remap to output
        # positions so every index in the report refers to `roots[]` as
        # returned to the caller.
        out_pos_of = {src: pos for pos, src in enumerate(order)}
        ev.clusters = [
            evidence_mod.Cluster(
                cluster_id=c.cluster_id,
                member_indices=sorted(out_pos_of[i] for i in c.member_indices),
                diameter=c.diameter,
            )
            for c in ev.clusters
        ]

        estimates = []
        for out_pos, src in enumerate(order):
            est = RootEstimate(
                root=complex(roots[src]),
                converged=bool(converged[src]),
                iterations=int(iterations[src]),
                residual_abs=float(res_abs[src]),
                residual_rel=float(res_rel[src]),
                error_bound=float(error_bound[src]),
            )
            estimates.append(est)

        all_converged = bool(np.all(converged))
        status = SolveStatus.CONVERGED if all_converged else SolveStatus.PARTIAL
        if all_converged:
            message = "all roots converged within the requested tolerance"
            reason = "all per-root convergence flags set"
        else:
            n_bad = int(np.sum(~converged))
            message = (
                f"iteration budget exhausted: {n_bad} of {poly.degree} roots did "
                "not meet the convergence tolerance; their last iterates are "
                "returned with converged=false"
            )
            reason = (
                f"max_iter={options.max_iter} exhausted with {n_bad} unconverged "
                "roots; state preserved rather than discarded"
            )

        self.logger.log(
            run_id, "decision",
            status=status.value,
            reason=reason,
            max_residual_rel=ev.max_residual_rel,
            reconstruction_error=ev.reconstruction_error,
            vieta_max_deviation=ev.vieta_max_deviation,
            n_clusters=len(ev.clusters),
        )

        report = SolveReport(
            run_id=run_id,
            status=status,
            degree=poly.degree,
            roots=estimates,
            evidence=ev,
            pairs=pairs,
            unpaired=unpaired,
            pair_tol=options.pair_tol,
            leading_dropped=poly.leading_dropped,
            message=message,
        )
        return report_to_dict(report)


def _finite(value: float):
    """JSON has no inf/nan; report them as strings rather than failing the
    response or silently substituting a number."""
    return value if math.isfinite(value) else repr(value)


def report_to_dict(report: SolveReport) -> dict:
    return {
        "run_id": report.run_id,
        "status": report.status.value,
        "degree": report.degree,
        "message": report.message,
        "normalization": {"leading_dropped": report.leading_dropped},
        "roots": [
            {
                "index": i,
                "re": _finite(r.root.real),
                "im": _finite(r.root.imag),
                "converged": r.converged,
                "iterations": r.iterations,
                "residual_abs": _finite(r.residual_abs),
                "residual_rel": _finite(r.residual_rel),
                "error_bound": _finite(r.error_bound),
            }
            for i, r in enumerate(report.roots)
        ],
        "evidence": {
            "max_residual_rel": _finite(report.evidence.max_residual_rel),
            "reconstruction_error": _finite(report.evidence.reconstruction_error),
            "vieta_max_deviation": _finite(report.evidence.vieta_max_deviation),
            "clusters": [
                {
                    "cluster_id": c.cluster_id,
                    "member_indices": c.member_indices,
                    "size": len(c.member_indices),
                    "diameter": c.diameter,
                }
                for c in report.evidence.clusters
            ],
            "accuracy_note": report.evidence.accuracy_note,
        },
        "pairing": {
            "pair_tol": report.pair_tol,
            "pairs": report.pairs,
            "unpaired": report.unpaired,
        },
    }
