"""Evidence orchestration: combine the exact result with independent witnesses.

Three witnesses of strictly separated authority:

1. **Exact Sturm** (the system under test) produces the intervals and proofs.
2. **Exact Descartes** (different theorem, no shared code with Sturm) plus
   structural invariants (pairwise disjointness, half-open membership, degree
   accounting) are the *authoritative* cross-check. Their disagreement means
   the answer is WRONG -> ``rejected``.
3. **Numeric** witnesses (mpmath high precision, NumPy/SciPy float64) only
   corroborate. If they cannot judge at their precision the answer becomes
   ``indeterminate``, never silently accepted and never overruling the exact
   evidence.

Verdicts
--------
``accepted``       exact checks pass and BOTH numeric witnesses confirm;
``indeterminate``  exact checks pass but a numeric witness could not confirm;
``rejected``       an exact (Descartes / structural) check failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from config.settings import NumericConfig
from root_isolator.evidence.descartes import (
    DescartesEvidence,
    descartes_check,
)
from root_isolator.evidence.float64_check import Float64Check, cross_check_float64
from root_isolator.evidence.numeric_check import MpmathReport, cross_check
from root_isolator.kernel.isolate import IsolationResult
from root_isolator.kernel.polynomial import RationalPoly
from root_isolator.kernel.squarefree import square_free_factors


@dataclass(frozen=True)
class IntervalEvidenceBundle:
    interval_index: int
    left: Fraction
    right: Fraction
    multiplicity: int
    sturm: dict[str, Any]
    descartes: DescartesEvidence
    mpmath: dict[str, Any]
    exact_descartes_agrees: bool
    numeric_confirmed: bool


@dataclass(frozen=True)
class Verdict:
    status: str  # "accepted" | "indeterminate" | "rejected"
    result_kind: str
    distinct_real_roots: int
    total_real_roots_with_multiplicity: int
    complex_roots_with_multiplicity: int
    intervals: tuple[IntervalEvidenceBundle, ...]
    float64: Float64Check | None
    mpmath: MpmathReport | None
    checks: tuple[dict[str, Any], ...]
    reasons: tuple[str, ...]


def _radical_of(polynomial: RationalPoly) -> RationalPoly:
    factors = square_free_factors(polynomial)
    radical = factors[0][1]
    for _, part in factors[1:]:
        radical = radical * part
    return radical


def _check_disjoint(intervals: list[tuple[Fraction, Fraction]]) -> dict[str, Any]:
    ordered = sorted(intervals)
    for (_, right), (next_left, _) in zip(ordered, ordered[1:]):
        if right > next_left:
            return {
                "name": "pairwise_disjoint",
                "passed": False,
                "detail": f"overlapping intervals between {right} and {next_left}",
            }
    return {
        "name": "pairwise_disjoint",
        "passed": True,
        "detail": f"{len(ordered)} intervals pairwise disjoint",
    }


def _check_sturm_proof(interval: Any) -> dict[str, Any]:
    delta = interval.proof.variations_left - interval.proof.variations_right
    passed = delta == 1 == interval.proof.distinct_roots
    return {
        "name": "sturm_variation_count",
        "passed": passed,
        "detail": (
            f"V(a+)={interval.proof.variations_left}, "
            f"V(b+)={interval.proof.variations_right}, count={delta}, depth={interval.proof.depth}"
        ),
    }


def _special_verdict(result: IsolationResult, name: str, detail: str) -> Verdict:
    return Verdict(
        status="accepted",
        result_kind=result.kind,
        distinct_real_roots=0,
        total_real_roots_with_multiplicity=0,
        complex_roots_with_multiplicity=0,
        intervals=(),
        float64=None,
        mpmath=None,
        checks=({"name": name, "passed": True, "detail": detail},),
        reasons=result.notes,
    )


def build_verdict(
    polynomial: RationalPoly,
    result: IsolationResult,
    numeric: NumericConfig,
) -> Verdict:
    """Collect all evidence and decide an accept/reject/indeterminate verdict."""

    if result.kind == "zero_polynomial":
        return _special_verdict(
            result,
            "zero_polynomial",
            "zero polynomial handled outside the ordinary root list",
        )
    if result.kind == "constant_nonzero":
        return _special_verdict(
            result,
            "constant_nonzero",
            "non-zero constant has degree 0 and no roots",
        )

    radical = _radical_of(polynomial)
    interval_triples = [
        (iv.left, iv.right, iv.multiplicity) for iv in result.intervals
    ]

    # --- authoritative exact witness: Descartes per interval ----------------
    exact_failures: list[str] = []
    descartes_results: list[DescartesEvidence] = []
    for index, interval in enumerate(result.intervals):
        evidence = descartes_check(
            radical,
            interval.left,
            interval.right,
            expect_root_at_right=interval.proof.right_endpoint_is_root,
        )
        descartes_results.append(evidence)
        if not evidence.agrees:
            exact_failures.append(f"interval {index}: {evidence.reason}")

    # --- authoritative structural invariants -------------------------------
    disjoint_check = _check_disjoint([(a, b) for a, b, _ in interval_triples])
    if not disjoint_check["passed"]:
        exact_failures.append(disjoint_check["detail"])

    sturm_checks = [_check_sturm_proof(iv) for iv in result.intervals]
    for check in sturm_checks:
        if not check["passed"]:
            exact_failures.append(check["detail"])

    complex_count = result.degree - result.total_real_roots_with_multiplicity
    multiplicity_check = {
        "name": "degree_accounting",
        "passed": complex_count >= 0
        and result.total_real_roots_with_multiplicity + complex_count == result.degree,
        "detail": (
            f"degree={result.degree}, real roots with multiplicity="
            f"{result.total_real_roots_with_multiplicity}, complex={complex_count}"
        ),
    }
    if not multiplicity_check["passed"]:
        exact_failures.append(multiplicity_check["detail"])

    # --- numeric (non-authoritative) witnesses ------------------------------
    mp_report = cross_check(polynomial, radical, interval_triples, numeric)
    float64 = cross_check_float64(polynomial, interval_triples)

    bundles: list[IntervalEvidenceBundle] = []
    for index, interval in enumerate(result.intervals):
        mp_interval = mp_report.interval_checks[index]
        bundles.append(
            IntervalEvidenceBundle(
                interval_index=index,
                left=interval.left,
                right=interval.right,
                multiplicity=interval.multiplicity,
                sturm={
                    "variations_left": interval.proof.variations_left,
                    "variations_right": interval.proof.variations_right,
                    "distinct_roots": interval.proof.distinct_roots,
                    "depth": interval.proof.depth,
                    "signs_left": interval.proof.signs_left,
                    "signs_right": interval.proof.signs_right,
                    "left_endpoint_is_root": interval.proof.left_endpoint_is_root,
                    "right_endpoint_is_root": interval.proof.right_endpoint_is_root,
                    "multiplicity_evidence": interval.proof.multiplicity_evidence,
                },
                descartes=descartes_results[index],
                mpmath={
                    "status": mp_interval.status,
                    "reason": mp_interval.reason,
                    "approximate_root": mp_interval.approximate_root,
                    "residual": mp_interval.residual,
                    "distance_to_left": mp_interval.distance_to_left,
                    "distance_to_right": mp_interval.distance_to_right,
                    "cluster_size": mp_interval.cluster_size,
                },
                exact_descartes_agrees=descartes_results[index].agrees,
                numeric_confirmed=mp_interval.status == "confirmed",
            )
        )

    checks: list[dict[str, Any]] = [disjoint_check, multiplicity_check]
    checks.extend(
        {
            "name": f"descartes[{i}]",
            "passed": evidence.agrees,
            "detail": evidence.reason,
        }
        for i, evidence in enumerate(descartes_results)
    )
    checks.extend(sturm_checks)
    checks.append(
        {"name": "mpmath_witness", "passed": mp_report.status == "confirmed", "detail": mp_report.reason}
    )
    checks.append(
        {"name": "float64_witness", "passed": float64.status == "confirmed", "detail": float64.reason}
    )

    numeric_failures: list[str] = []
    if mp_report.status != "confirmed":
        numeric_failures.append(f"mpmath: {mp_report.reason}")
    if float64.status != "confirmed":
        numeric_failures.append(f"float64: {float64.reason}")

    if exact_failures:
        status = "rejected"
        reasons = tuple(exact_failures + numeric_failures)
    elif numeric_failures:
        status = "indeterminate"
        reasons = tuple(numeric_failures)
    else:
        status = "accepted"
        reasons = (
            f"all {len(bundles)} interval(s) confirmed by exact Descartes, "
            "mpmath high-precision and float64 witnesses",
        )

    return Verdict(
        status=status,
        result_kind="isolated",
        distinct_real_roots=result.distinct_real_roots,
        total_real_roots_with_multiplicity=result.total_real_roots_with_multiplicity,
        complex_roots_with_multiplicity=complex_count,
        intervals=tuple(bundles),
        float64=float64,
        mpmath=mp_report,
        checks=tuple(checks),
        reasons=reasons,
    )
