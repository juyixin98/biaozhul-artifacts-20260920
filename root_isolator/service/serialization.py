"""Serialization of exact results and evidence into JSON-safe structures.

Rationals are emitted as ``{"numerator": ..., "denominator": ..., "decimal":
...}`` where ``decimal`` is a *display-only* approximation clearly labelled as
such; the numerator/denominator pair is the authoritative value.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from root_isolator.evidence.descartes import DescartesEvidence
from root_isolator.evidence.verifier import Verdict


def _rational(value: Fraction, *, decimal_digits: int = 30) -> dict[str, Any]:
    return {
        "numerator": value.numerator,
        "denominator": value.denominator,
        "decimal_display": _decimal(value, decimal_digits),
    }


def _decimal(value: Fraction, digits: int) -> str:
    # Exact rational -> bounded decimal expansion with a remainder marker, so the
    # display never pretends to be more precise than it is.
    sign = "-" if value < 0 else ""
    value = abs(value)
    whole, remainder = divmod(value.numerator, value.denominator)
    if remainder == 0:
        return f"{sign}{whole}"
    digits_out: list[str] = []
    for _ in range(digits):
        remainder *= 10
        digit, remainder = divmod(remainder, value.denominator)
        digits_out.append(str(digit))
        if remainder == 0:
            return f"{sign}{whole}." + "".join(digits_out)
    return f"{sign}{whole}." + "".join(digits_out) + "..."


def _descartes(evidence: DescartesEvidence) -> dict[str, Any]:
    return {
        "theorem": "exact Descartes/Vincent sign-rule bisection (independent of Sturm)",
        "root_at_right_endpoint": evidence.root_at_right_endpoint,
        "descartes_root_count": evidence.descartes_root_count,
        "bisection_nodes": evidence.bisection_nodes,
        "conclusive": evidence.conclusive,
        "agrees": evidence.agrees,
        "reason": evidence.reason,
    }


def serialize_verdict(verdict: Verdict, *, request_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    intervals = []
    for bundle in verdict.intervals:
        intervals.append(
            {
                "index": bundle.interval_index,
                "left": _rational(bundle.left),
                "right": _rational(bundle.right),
                "multiplicity": bundle.multiplicity,
                "proof": {
                    "theorem": "Sturm",
                    "convention": "half-open (a, b]; count = V(a+) - V(b+)",
                    **bundle.sturm,
                },
                "independent_evidence": {
                    "descartes": _descartes(bundle.descartes),
                    "mpmath": bundle.mpmath,
                },
            }
        )

    return {
        "request_id": request_id,
        "verdict": verdict.status,
        "result_kind": verdict.result_kind,
        "root_counts": {
            "distinct_real_roots": verdict.distinct_real_roots,
            "real_roots_with_multiplicity": verdict.total_real_roots_with_multiplicity,
            "complex_roots_with_multiplicity": verdict.complex_roots_with_multiplicity,
        },
        "intervals": intervals,
        "evidence": {
            "checks": list(verdict.checks),
            "reasons": list(verdict.reasons),
            "float64_witness": (
                None
                if verdict.float64 is None
                else {
                    "status": verdict.float64.status,
                    "reason": verdict.float64.reason,
                    "distinct_real_roots_float64": verdict.float64.distinct_real_roots_float64,
                    "matched_intervals": verdict.float64.matched_intervals,
                    "unmatched_intervals": verdict.float64.unmatched_intervals,
                    "unassigned_roots": list(verdict.float64.unassigned_roots),
                    "approximate_roots": list(verdict.float64.approximate_roots),
                }
            ),
            "mpmath_distinct_real_roots": (
                None if verdict.mpmath is None else verdict.mpmath.distinct_real_roots
            ),
        },
        "meta": meta,
    }


def serialize_error(
    *,
    request_id: str,
    category: str,
    message: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Uniform error envelope; never includes raw coefficient payloads."""

    return {
        "request_id": request_id,
        "verdict": "rejected",
        "error": {
            "category": category,
            "message": message,
            "state": state,
        },
    }
