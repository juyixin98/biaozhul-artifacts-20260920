"""Orchestration: run every method under several rearrangements and adjudicate.

This service is the only place that knows the full experiment matrix:

* orderings  - original, reversed, block-reversed, abs-sorted, shuffled;
* methods    - naive, Kahan, pairwise (monolithic);
* chunked    - the same ideas driven block-by-block, with compensation
               state genuinely carried between blocks.

The high-precision oracle is computed once on the unordered multiset of
exact binary64 values (its sum is permutation-invariant), never by any
kernel under test.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..config import settings
from ..core import chunking, kernels
from ..core.blas_utils import sum_abs
from ..core.errors import (
    ErrorAssessment,
    ReferenceValue,
    assess_error,
    kahan_bound,
    naive_bound,
    pairwise_bound,
    reference_sum,
)
from .diagnostics import log_event, summarize_input

#: Element-level rearrangements offered by the API.
ORDERINGS = ("original", "reversed", "blocks_reversed", "abs_ascending", "abs_descending", "shuffle")

MONOLITHIC_METHODS = ("naive", "kahan", "pairwise")
# ``naive_sharded`` is the documented anti-pattern (local totals added with
# ordinary float adds); kept in the matrix so the accuracy gap is visible.
CHUNKED_METHODS = (
    "blocked_naive",
    "naive_sharded",
    "kahan_streaming",
    "kahan_merged",
    "blocked_pairwise",
)


@dataclass(frozen=True)
class OrderingSpec:
    name: str
    shuffle_seed: int = 0
    block_size: int = 4096


def permute(values: np.ndarray, spec: OrderingSpec) -> np.ndarray:
    """Return a rearranged copy of ``values``."""
    if spec.name == "original":
        return values
    if spec.name == "reversed":
        return values[::-1].copy()
    if spec.name == "blocks_reversed":
        b = spec.block_size
        pieces = [values[i : i + b] for i in range(0, values.size, b)]
        return np.concatenate(pieces[::-1]) if pieces else values.copy()
    if spec.name in ("abs_ascending", "abs_descending"):
        order = np.argsort(np.abs(values), kind="stable")
        if spec.name == "abs_descending":
            order = order[::-1]
        return values[order].copy()
    if spec.name == "shuffle":
        rng = np.random.default_rng(spec.shuffle_seed)
        order = rng.permutation(values.size)
        return values[order].copy()
    raise ValueError(f"unknown ordering {spec.name!r}")


def _run_monolithic(name: str, values: np.ndarray, info: kernels.SpecialAssessment) -> float:
    if name == "naive":
        return kernels.naive_sum(values, info)
    if name == "kahan":
        return kernels.kahan_sum(values, info)
    if name == "pairwise":
        return kernels.pairwise_sum(values, info)
    raise ValueError(name)


def _run_chunked(name: str, values: np.ndarray, block_size: int) -> float:
    if name == "blocked_naive":
        return chunking.blocked_naive(values, block_size)
    if name == "naive_sharded":
        return chunking.naive_sharded_totals(values, block_size)
    if name == "kahan_streaming":
        return chunking.blocked_kahan_streaming(values, block_size)
    if name == "kahan_merged":
        return chunking.blocked_kahan_merged(values, block_size)
    if name == "blocked_pairwise":
        return chunking.blocked_pairwise(values, block_size)
    raise ValueError(name)


def _monolithic_bound(method: str, n: int, sum_abs: float) -> float:
    if method == "naive":
        return naive_bound(n, sum_abs)
    if method == "kahan":
        return kahan_bound(n, sum_abs)
    return pairwise_bound(n, kernels.LEAF_SIZE, sum_abs)


def _chunked_bound(method: str, n: int, block_size: int, sum_abs: float) -> float:
    if method == "blocked_pairwise":
        return pairwise_bound(n, block_size, sum_abs)
    if method in ("kahan_streaming", "kahan_merged"):
        return kahan_bound(n, sum_abs)
    # blocked_naive and the naive_sharded anti-pattern both pay full O(n*u).
    return naive_bound(n, sum_abs)


def _special_entry(method: str, result: float, policy_note: str) -> ErrorAssessment:
    from ..core.errors import Verdict

    return ErrorAssessment(
        method=method,
        result=result,
        abs_error=math.nan,
        rel_error=None,
        theoretical_bound=None,
        bound_ratio=None,
        condition_number=None,
        verdict=Verdict.ACCEPTED,
        reasons=[f"fixed special-value policy applied: {policy_note}"],
    )


def run_comparison(
    values: np.ndarray,
    block_size: int,
    ordering_names: list[str],
    shuffle_seed: int = 0,
) -> dict[str, Any]:
    """Execute the full comparison matrix and return a JSON-ready payload."""
    n = values.size
    info = kernels.assess(values)
    special = kernels.resolve_special(info)
    sum_abs_value = sum_abs(values) if special is None else math.nan

    reference: ReferenceValue | None = None
    if special is None:
        reference = reference_sum(values)
        log_event(
            "reference_computed",
            method=reference.method.value,
            precision=reference.precision_digits,
            uncertainty=reference.abs_uncertainty,
        )
    else:
        log_event("special_value_policy", has_nan=info.has_nan, pos_inf=info.pos_inf, neg_inf=info.neg_inf)

    orderings_payload: dict[str, Any] = {}
    for name in ordering_names:
        spec = OrderingSpec(name=name, shuffle_seed=shuffle_seed, block_size=block_size)
        arranged = permute(values, spec)
        ordered_info = kernels.assess(arranged)  # census identical; cheap, kept explicit
        ordered_special = kernels.resolve_special(ordered_info)

        methods_payload: dict[str, Any] = {}
        mono_results: dict[str, float] = {}
        for method in MONOLITHIC_METHODS:
            result = _run_monolithic(method, arranged, ordered_info)
            mono_results[method] = result
            if ordered_special is not None:
                assessment = _special_entry(method, result, _policy_note(ordered_info))
            else:
                bound = _monolithic_bound(method, n, sum_abs_value)
                assessment = assess_error(method, result, reference, n, sum_abs_value, bound)
            methods_payload[method] = assessment.to_plain()
            log_event(
                "verdict",
                ordering=name,
                scope="monolithic",
                method=method,
                verdict=assessment.verdict.value,
                abs_error=None if math.isnan(assessment.abs_error) else assessment.abs_error,
            )

        chunked_payload: dict[str, Any] = {}
        chunked_results: dict[str, float] = {}
        for method in CHUNKED_METHODS:
            result = _run_chunked(method, arranged, block_size)
            chunked_results[method] = result
            if ordered_special is not None:
                assessment = _special_entry(method, result, _policy_note(ordered_info))
            else:
                bound = _chunked_bound(method, n, block_size, sum_abs_value)
                assessment = assess_error(method, result, reference, n, sum_abs_value, bound)
            chunked_payload[method] = assessment.to_plain()
            log_event(
                "verdict",
                ordering=name,
                scope="chunked",
                method=method,
                verdict=assessment.verdict.value,
                abs_error=None if math.isnan(assessment.abs_error) else assessment.abs_error,
            )

        orderings_payload[name] = {
            "methods": methods_payload,
            "chunked": chunked_payload,
            "invariants": (
                _invariants(mono_results, chunked_results, reference, n, sum_abs_value, block_size)
                if ordered_special is None
                else {}
            ),
        }

    return {
        "n": n,
        "block_size": block_size,
        "orderings_requested": list(ordering_names),
        "input_summary": summarize_input(values),
        "special_value_policy": _policy_payload(info, special),
        "reference": None if reference is None else reference.to_plain(),
        "orderings": orderings_payload,
    }


def _invariants(
    mono: dict[str, float],
    chunked: dict[str, float],
    reference: ReferenceValue,
    n: int,
    sum_abs: float,
    block_size: int,
) -> dict[str, Any]:
    """Structural identities a correct implementation must satisfy.

    The first two are bit-exact equalities of floating results.  The third
    is an error-order claim: sharded double-double merge must achieve the
    *compensated* bound, not merely the naive bound - this is the concrete
    test that compensation state genuinely crossed the block boundary.
    """
    merged_error = abs(chunked["kahan_merged"] - reference.value)
    compensated_bound = kahan_bound(n, sum_abs)
    return {
        # Naive has no state beyond the running total.
        "blocked_naive_equals_naive": chunked["blocked_naive"] == mono["naive"],
        # A continuous compensation thread is a monolithic Kahan pass.
        "kahan_streaming_equals_monolithic": chunked["kahan_streaming"] == mono["kahan"],
        # Independent shards combined as double-doubles keep O(u) accuracy;
        # a local-totals-only reduction would be bounded by the O(n*u)
        # naive bound and fail this comparison on ill-conditioned inputs.
        "kahan_merged_meets_compensated_bound": (
            math.isfinite(merged_error) and merged_error <= settings.error_tolerance_factor * compensated_bound
        ),
        "kahan_merged_abs_error": merged_error,
        "kahan_compensated_abs_bound": compensated_bound,
        "naive_abs_bound": naive_bound(n, sum_abs),
        "naive_sharded_abs_error": abs(chunked["naive_sharded"] - reference.value),
    }


def _policy_note(info: kernels.SpecialAssessment) -> str:
    if info.has_nan:
        return "NaN present -> result is quiet NaN"
    if info.pos_inf and info.neg_inf:
        return "both +Infinity and -Infinity present -> invalid operation, quiet NaN"
    if info.pos_inf:
        return "only +Infinity present -> +Infinity"
    if info.neg_inf:
        return "only -Infinity present -> -Infinity"
    return "finite input"


def _policy_payload(info: kernels.SpecialAssessment, special: float | None) -> dict[str, Any] | None:
    if special is None:
        return None
    return {
        "triggered": True,
        "result_is_nan": isinstance(special, float) and math.isnan(special),
        "note": _policy_note(info),
        "census": {
            "nan": int(info.has_nan),
            "plus_infinity": info.pos_inf,
            "minus_infinity": info.neg_inf,
            "plus_zero": info.pos_zero,
            "minus_zero": info.neg_zero,
            "nonzero_finite": info.nonzero_finite,
        },
        "signed_zero_rule": "all-zero input containing -0.0 and no +0.0 sums to -0.0; otherwise +0.0",
    }
