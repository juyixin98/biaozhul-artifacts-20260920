"""Framework-agnostic application orchestration.

Connects the four real layers without depending on HTTP:
input parsing -> exact Sturm kernel -> independent evidence -> verdict.
The FastAPI package is a thin adapter over :func:`isolate_from_payload`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config.settings import BudgetConfig, NumericConfig
from root_isolator.errors import ErrorCategory, IsolationError
from root_isolator.evidence.verifier import Verdict, build_verdict
from root_isolator.input.parser import (
    parse_dense_coefficients,
    parse_sparse_coefficients,
)
from root_isolator.kernel.isolate import isolate_roots
from root_isolator.service.diagnostics import Diagnostics


@dataclass(frozen=True)
class Outcome:
    verdict: Verdict
    fingerprint: str
    meta: dict[str, Any]


def _parse(payload: dict[str, Any], budget: BudgetConfig):
    if "coefficients" in payload:
        return parse_dense_coefficients(
            payload["coefficients"],
            order=payload.get("order", "descending"),
            budget=budget,
        )
    if "sparse" in payload:
        return parse_sparse_coefficients(payload["sparse"], budget=budget)
    raise IsolationError(
        ErrorCategory.MALFORMED_COEFFICIENTS,
        "request must contain either 'coefficients' (dense list) or 'sparse' (power map)",
        state={"keys": sorted(payload.keys())},
    )


def isolate_from_payload(
    payload: dict[str, Any],
    *,
    budget: BudgetConfig,
    numeric: NumericConfig,
    diagnostics: Diagnostics,
) -> Outcome:
    """Run the full pipeline; raises :class:`IsolationError` on rejection."""

    diagnostics.info("request_received", "isolation request received")
    parsed = _parse(payload, budget)
    diagnostics.info(
        "parsed",
        "coefficients parsed exactly",
        fingerprint=parsed.fingerprint,
        degree=parsed.effective_degree,
        terms=parsed.nonzero_terms,
        is_zero=parsed.is_zero,
        # raw coefficients deliberately omitted: see diagnostics redaction
        coefficients=payload.get("coefficients", payload.get("sparse")),
    )

    result = isolate_roots(parsed.polynomial, budget)
    verdict = build_verdict(parsed.polynomial, result, numeric)

    state = {
        "degree": result.degree,
        "distinct_real_roots": verdict.distinct_real_roots,
        "total_real_roots_with_multiplicity": verdict.total_real_roots_with_multiplicity,
        "result_kind": verdict.result_kind,
        "evaluations": result.evaluations,
        "max_depth": result.max_depth,
    }
    if verdict.status == "accepted":
        diagnostics.accepted(parsed.fingerprint, **state)
    elif verdict.status == "indeterminate":
        diagnostics.indeterminate(parsed.fingerprint, list(verdict.reasons), **state)
    else:
        diagnostics.rejected(parsed.fingerprint, list(verdict.reasons), **state)

    meta = {
        "fingerprint": parsed.fingerprint,
        "declared_degree": parsed.declared_degree,
        "effective_degree": parsed.effective_degree,
        "nonzero_terms": parsed.nonzero_terms,
        "max_coefficient_bits": parsed.max_coefficient_bits,
        "cauchy_bound": result.cauchy_bound,
        "sturm_chain_length": result.chain_length,
        "sturm_evaluations": result.evaluations,
        "max_bisection_depth": result.max_depth,
        "square_free_factors": [
            {"multiplicity": m, "degree": d} for m, d in result.factors
        ],
        "budget": {
            "max_degree": budget.max_degree,
            "max_coefficient_bits": budget.max_coefficient_bits,
            "max_sturm_pairs": budget.max_sturm_pairs,
            "max_bisection_depth": budget.max_bisection_depth,
            "max_roots": budget.max_roots,
        },
    }
    return Outcome(verdict=verdict, fingerprint=parsed.fingerprint, meta=meta)
