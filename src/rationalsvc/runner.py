"""Orchestration: budgets, run identity, checkpoints and replayable logs.

Flow::

    ParsedSystem
      -> clear denominators per augmented row (exact, sign preserving)
      -> kernel.eliminate with a digit-budget hook
      -> evidence.extract_solutions + evidence.independently_verify
      -> (optional) evidence.float64_diagnosis
      -> JSON-safe result dict

Every run gets a ``run_id``; a JSONL record keeps the exact input, every
kernel checkpoint (pivot step, swaps, integer sizes), the judgement reason and
the outcome.  When the digit budget is exhausted mid-elimination the hook
raises :class:`BudgetExhausted` whose ``details.progress`` contains the last
:class:`ProgressSnapshot` - the failure is diagnosable and replayable rather
than silently degrading to float.
"""
from __future__ import annotations

import json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any

from . import evidence
from .errors import BudgetExhausted, ComputationFailed
from .frac import common_denominator
from .kernel import ProgressSnapshot, eliminate
from .numeric_input import ParsedSystem


# ---------------------------------------------------------------------------
# Run identity + logging
# ---------------------------------------------------------------------------

def new_run_id() -> str:
    return "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + \
        "-" + secrets.token_hex(3)


class RunLogger:
    """Append-only JSONL run log.  One line per event, keyed by run_id."""

    def __init__(self, path: str | None) -> None:
        self.path = path

    def event(self, run_id: str, kind: str, **payload: Any) -> dict:
        rec = {
            "run_id": run_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": kind,
            **payload,
        }
        if self.path:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        return rec


# ---------------------------------------------------------------------------
# JSON-safe exact-number encoding
# ---------------------------------------------------------------------------

def qstr(v: Fraction) -> str:
    """Canonical exact scalar string: ``"7"`` or ``"-3/5"`` (never decimal)."""
    return str(v)


def _exact_input_snapshot(parsed: ParsedSystem) -> dict:
    return {
        "A": [[qstr(v) for v in row] for row in parsed.A],
        "b": [[qstr(v) for v in row] for row in parsed.b],
        "m": parsed.m,
        "n": parsed.n,
        "rhs_count": parsed.rhs_count,
        "digit_budget": parsed.digit_budget,
        "want": parsed.want,
    }


def _integer_system(parsed: ParsedSystem):
    """Exact clearing of denominators on each augmented row.

    Returns (integer rows, row multipliers).  A positive multiplier can never
    flip a sign; the multiplier itself is logged for replay.
    """
    int_rows: list[list[int]] = []
    multipliers: list[int] = []
    for i in range(parsed.m):
        rational_row = list(parsed.A[i]) + list(parsed.b[i])
        mult = common_denominator(rational_row)
        multipliers.append(mult)
        int_rows.append([int(v * mult) for v in rational_row])
    return int_rows, multipliers


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

@dataclass
class BudgetStatus:
    limit_digits: int
    observed_max_digits: int = 0


def _make_budget_hook(run_id: str, limit: int, status: BudgetStatus,
                      logger: RunLogger):
    def hook(snap: ProgressSnapshot) -> None:
        status.observed_max_digits = max(
            status.observed_max_digits, snap.max_decimal_digits
        )
        logger.event(
            run_id, "checkpoint",
            phase=snap.phase,
            step=snap.step,
            column=snap.column,
            rank_so_far=snap.rank_so_far,
            swaps=snap.swaps,
            max_decimal_digits=snap.max_decimal_digits,
            digit_budget=limit,
        )
        if snap.max_decimal_digits > limit:
            progress = snap.to_dict()
            progress["reason"] = (
                f"intermediate coefficient has {snap.max_decimal_digits} "
                f"decimal digits, exceeding the budget of {limit}; computation "
                "stopped while still exact (no floating-point fallback exists)"
            )
            progress["observed_max_digits"] = status.observed_max_digits
            logger.event(
                run_id, "budget_exhausted",
                limit_digits=limit,
                observed_max_digits=snap.max_decimal_digits,
                progress=progress,
            )
            raise BudgetExhausted(
                "digit budget exhausted during exact elimination",
                {"run_id": run_id, "progress": progress,
                 "limit_digits": limit,
                 "observed_max_digits": snap.max_decimal_digits},
            )
    return hook


# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------

def _solution_payload(sol: evidence.RhsSolution) -> dict:
    out: dict = {
        "index": sol.index,
        "classification": sol.classification,
        "rank_augmented": sol.rank_augmented,
    }
    if sol.classification == evidence.INCONSISTENT:
        assert sol.left_null_vector is not None
        out["contradiction_witness"] = {
            "witness_row_in_rref": sol.witness_row,
            # y^T A = 0 while y^T b != 0, in ORIGINAL equation coordinates:
            "y": [qstr(v) for v in sol.left_null_vector],
            "y_dot_b": qstr(sol.y_dot_b) if sol.y_dot_b is not None else None,
            "instruction": (
                "Form the linear combination sum_i y_i * (equation i); "
                "the left-hand side is identically 0 but the right-hand side "
                "equals y_dot_b (non-zero), proving inconsistency exactly."
            ),
        }
    else:
        out["particular"] = [qstr(v) for v in sol.particular]
        out["parametric_form"] = {
            "x": "x = particular + sum_t t_k * nullspace_basis[k]",
            "parameters": [f"t_{k}" for k in range(len(sol.nullspace_basis))],
            # free_columns is filled by the caller from kernel pivot data
            "free_columns": [],
            "nullspace_basis": [
                [qstr(v) for v in vec] for vec in sol.nullspace_basis
            ],
        }
    return out


def solve_system(
    parsed: ParsedSystem,
    logger: RunLogger,
    run_id: str | None = None,
    include_float_diagnosis: bool = True,
) -> dict:
    """Run the full exact pipeline; return a JSON-safe result envelope."""
    run_id = run_id or new_run_id()
    logger.event(run_id, "started", **_exact_input_snapshot(parsed))

    int_rows, multipliers = _integer_system(parsed)
    logger.event(
        run_id, "denominators_cleared",
        row_multipliers=[str(m) for m in multipliers],
        integer_augmented=[[str(v) for v in row] for row in int_rows],
    )

    status = BudgetStatus(limit_digits=parsed.digit_budget)
    hook = _make_budget_hook(run_id, parsed.digit_budget, status, logger)

    res = eliminate(
        int_rows, parsed.n, budget_hook=hook,
        input_row_multipliers=multipliers,
    )

    solutions = evidence.extract_solutions(res, parsed.b)
    verification = evidence.independently_verify(parsed.A, parsed.b, solutions)
    if not verification.all_residuals_zero:
        raise ComputationFailed(
            "internal verification failed: a claimed solution/witness does not "
            "satisfy the original equations exactly",
            {"run_id": run_id,
             "verification": _verification_payload(verification)},
        )

    free_cols = [c for c in range(parsed.n) if c not in set(res.pivot_columns)]

    result: dict[str, Any] = {
        "run_id": run_id,
        "status": "ok",
        "exact": True,
        "backend": "fraction-free Bareiss elimination over Python integers",
        "shape": {"rows": parsed.m, "columns": parsed.n,
                  "right_hand_sides": parsed.rhs_count},
        "rank": {
            "rank_A": res.rank,
            "nullity": parsed.n - res.rank,
            "pivot_columns": res.pivot_columns,
            "free_columns": free_cols,
            "row_swaps": [list(s) for s in res.swaps],
            "content_divisors_removed": [str(g) for g in res.content_divisors],
            "determinant": (
                None if res.determinant_original is None
                else str(res.determinant_original)
            ),
            "determinant_note": (
                "exact determinant of A (square, full rank only); "
                "row content factors restored, swap signs applied"
            ),
        },
        "budget": {
            "limit_digits": parsed.digit_budget,
            "observed_max_digits": status.observed_max_digits,
            "within_budget": status.observed_max_digits <= parsed.digit_budget,
        },
    }

    # want == "rank" returns structural information only; the augmented system
    # was still eliminated (so the rank is exact) but solutions are omitted.
    if parsed.want != "rank":
        payload_solutions = []
        for sol in solutions:
            d = _solution_payload(sol)
            if d["classification"] != evidence.INCONSISTENT:
                d["parametric_form"]["free_columns"] = free_cols
            payload_solutions.append(d)
        result["solutions"] = payload_solutions
        result["verification"] = _verification_payload(verification)
        if include_float_diagnosis:
            result["float_diagnosis"] = evidence.float64_diagnosis(
                parsed.A, parsed.b, res.rank, solutions
            )

    logger.event(
        run_id, "completed",
        rank=res.rank,
        classifications=[s.classification for s in solutions],
        observed_max_digits=status.observed_max_digits,
    )
    return result


def _verification_payload(v: evidence.VerificationReport) -> dict:
    return {
        "method": (
            "every particular vector and null-space vector was substituted "
            "into the ORIGINAL equations using an independent Fraction code "
            "path; contradiction witnesses were re-checked as y^T A = 0 and "
            "y^T b != 0"
        ),
        "particular_residuals": v.particular_residuals,
        "nullspace_residuals": v.nullspace_residuals,
        "witness_y_dot_A": v.witness_y_dot_A,
        "witness_y_dot_b": v.witness_y_dot_b,
        "all_residuals_zero": v.all_residuals_zero,
    }


def log_error(logger: RunLogger, run_id: str, category: str, message: str,
              details: dict) -> None:
    logger.event(run_id, "error", error=category, message=message,
                 details=_json_safe(details))


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, Fraction):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj
