"""Application orchestration: parse -> exact compute -> independent evidence.

This module is the single place where the data contract between layers is
assembled.  It returns plain JSON-serialisable payloads and raises only
:class:`~rational_linalg.errors.RationalLinAlgError` subclasses.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from ..budget import DigitBudget
from ..errors import InputError, ResourceExhaustedError
from ..evidence import (
    fraction_free_determinant,
    fraction_to_payload,
    verify_contradiction_witness,
    verify_left_nullspace,
    verify_solution,
)
from ..floatdiag import float_diagnosis
from ..numbers import parse_matrix, parse_vector
from ..run_log import RunRecorder
from ..solve import INCONSISTENT, rank_of, solve_augmented

Vec = list[Fraction]
Mat = list[Vec]


def _vec_payload(vector: Vec, dps: int) -> list[dict[str, Any]]:
    return [fraction_to_payload(v, dps=dps) for v in vector]


def _budget_payload(budget: DigitBudget) -> dict[str, Any]:
    snap = budget.snapshot().to_dict()
    snap["trace"] = [
        {"pivot": p, "max_digits": d} for p, d in budget.trace
    ]
    return snap


def _attach_progress(exc: ResourceExhaustedError, recorder: RunRecorder) -> None:
    """Ensure a budget failure carries replay info and is logged."""
    exc.details.setdefault("run_id", recorder.run_id)
    recorder.emit(
        {
            "event": "resource_exhausted",
            "code": exc.code,
            "message": exc.message,
            "progress_keys": sorted(exc.progress.keys()),
        }
    )


def solve_system(
    payload: Any, recorder: RunRecorder
) -> dict[str, Any]:
    A = parse_matrix(payload.A, name="A")
    b = parse_vector(payload.b, path="b")
    m, n = len(A), len(A[0])
    if len(b) != m:
        raise InputError(
            f"b has length {len(b)}, expected {m}",
            code="DIMENSION_MISMATCH",
            details={"rows": m, "b_len": len(b)},
        )

    budget = DigitBudget(digit_limit=payload.digit_budget)
    dps = payload.decimal_dps
    recorder.emit(
        {
            "event": "request_parsed",
            "shape": [m, n],
            "digit_budget": payload.digit_budget,
        }
    )

    try:
        result = solve_augmented(A, b, budget=budget, sink=recorder.emit)
    except ResourceExhaustedError as exc:
        _attach_progress(exc, recorder)
        raise

    classification = result["classification"]
    response: dict[str, Any] = {
        "run_id": recorder.run_id,
        "kind": "solve",
        "classification": classification,
        "shape": result["shape"],
        "rank": {"A": result["rank_a"], "augmented": result["rank_augmented"]},
        "pivot_columns": result["pivot_columns"],
        "row_swaps": result["row_swaps"],
        "pivot_signs": result["pivot_signs"],
        "budget": _budget_payload(result["budget"]),
        "arithmetic": "exact rational (fractions.Fraction); no float fallback",
    }

    if m == n:
        det = fraction_free_determinant(result["elimination"], n)
        response["determinant"] = (
            fraction_to_payload(det, dps=dps) if det is not None else None
        )

    if classification == INCONSISTENT:
        y = result["contradiction_witness"]
        evidence = verify_contradiction_witness(A, b, y)
        response["contradiction"] = {
            "witness_y": _vec_payload(y, dps),
            "yT_A": [f"{v.numerator}/{v.denominator}" for v in evidence["yt_A"]],
            "yT_b": fraction_to_payload(evidence["yt_b"], dps=dps),
            "annihilates_A": evidence["annihilates_a"],
            "contradicts_b": evidence["contradicts_b"],
            "evidence_ok": evidence["ok"],
            "echelon_row_index": result["witness_echelon_row"],
        }
    else:
        particular = result["particular"]
        null_basis = result["null_basis"]
        evidence = verify_solution(A, b, particular, null_basis)
        response["solution"] = {
            "particular": _vec_payload(particular, dps),
            "null_space_basis": [
                _vec_payload(v, dps) for v in null_basis
            ],
            "free_columns": result.get("free_columns", []),
            "parametric_form": (
                "x = particular + sum(t_j * null_space_basis[j]); t_j arbitrary rationals"
            ),
            "residual_Ap_minus_b": [
                f"{v.numerator}/{v.denominator}" for v in evidence["particular_residual"]
            ],
            "null_residuals": [
                [f"{v.numerator}/{v.denominator}" for v in r]
                for r in evidence["null_residuals"]
            ],
            "evidence_ok": evidence["ok"],
        }

    if payload.include_float_diagnosis:
        response["float_diagnosis"] = float_diagnosis(
            A, b, exact_rank=result["rank_a"]
        )

    recorder.complete(
        {
            "classification": classification,
            "rank": response["rank"],
            "evidence_ok": (
                response["contradiction"]["evidence_ok"]
                if classification == INCONSISTENT
                else response["solution"]["evidence_ok"]
            ),
        }
    )
    return response


def rank_system(payload: Any, recorder: RunRecorder) -> dict[str, Any]:
    A = parse_matrix(payload.A, name="A")
    m, n = len(A), len(A[0])
    budget = DigitBudget(digit_limit=payload.digit_budget)
    dps = payload.decimal_dps
    recorder.emit(
        {
            "event": "request_parsed",
            "shape": [m, n],
            "digit_budget": payload.digit_budget,
        }
    )

    try:
        result = rank_of(A, budget=budget, sink=recorder.emit)
    except ResourceExhaustedError as exc:
        _attach_progress(exc, recorder)
        raise

    evidence = verify_left_nullspace(A, result["left_nullspace"])
    response: dict[str, Any] = {
        "run_id": recorder.run_id,
        "kind": "rank",
        "shape": result["shape"],
        "rank": result["rank"],
        "nullity": result["nullity"],
        "left_nullity": result["left_nullity"],
        "pivot_columns": result["pivot_columns"],
        "row_swaps": result["row_swaps"],
        "pivot_signs": result["pivot_signs"],
        "left_nullspace": [
            _vec_payload(y, dps) for y in result["left_nullspace"]
        ],
        "left_nullspace_residuals": [
            [f"{v.numerator}/{v.denominator}" for v in r]
            for r in evidence["residuals"]
        ],
        "evidence_ok": evidence["ok"],
        "budget": _budget_payload(result["budget"]),
        "arithmetic": "exact rational (fractions.Fraction); no float fallback",
    }
    if m == n:
        det = fraction_free_determinant(result["elimination"], n)
        response["determinant"] = (
            fraction_to_payload(det, dps=dps) if det is not None else None
        )
    if payload.include_float_diagnosis:
        response["float_diagnosis"] = float_diagnosis(
            A, exact_rank=result["rank"]
        )

    recorder.complete(
        {"rank": result["rank"], "evidence_ok": evidence["ok"]}
    )
    return response
