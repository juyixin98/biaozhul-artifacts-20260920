"""Sturm-based exact real-root isolation.

Root counting convention
------------------------
Every interval is the half-open rational interval ``(a, b]``. At a point where
the polynomial itself vanishes we *delete the leading zero* from the Sturm
sign sequence, which evaluates the right-hand limit ``V(x+)`` of the
variation-count step function. Consequently::

    # {roots in (a, b]} = V(a+) - V(b+)

so a root placed exactly at the left endpoint ``a`` is excluded and a root
placed exactly at the right endpoint ``b`` is included. Bisection therefore
needs no special case when its midpoint happens to be an exact root: that root
is attributed to the left child by the same convention.

Disjointness and multiplicities
-------------------------------
To guarantee pairwise disjoint output intervals even with repeated roots, we
isolate the *radical* ``W = f / gcd(f, f')`` (one copy of every distinct
root), yielding disjoint intervals. Each interval is then assigned its
multiplicity by counting roots of each square-free factor ``F_k`` inside it;
exactly one factor owns the unique root.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from config.settings import BudgetConfig
from root_isolator.errors import ErrorCategory, IsolationError
from root_isolator.kernel.euclidean import (
    sign_sequence,
    sturm_chain,
    variations_from_signs,
)
from root_isolator.kernel.polynomial import RationalPoly
from root_isolator.kernel.squarefree import cauchy_integer_bound, square_free_factors


@dataclass(frozen=True)
class IntervalProof:
    """Why an interval isolates exactly one distinct root."""

    signs_left: list[int]
    signs_right: list[int]
    variations_left: int
    variations_right: int
    distinct_roots: int
    depth: int
    left_endpoint_is_root: bool
    right_endpoint_is_root: bool
    multiplicity_evidence: dict[str, Any]


@dataclass(frozen=True)
class RootInterval:
    left: Fraction
    right: Fraction
    multiplicity: int
    proof: IntervalProof

    def to_rationals(self) -> dict[str, dict[str, int]]:
        return {
            "left": {"numerator": self.left.numerator, "denominator": self.left.denominator},
            "right": {"numerator": self.right.numerator, "denominator": self.right.denominator},
        }


@dataclass(frozen=True)
class IsolationResult:
    kind: str  # "isolated" | "constant_nonzero" | "zero_polynomial"
    intervals: tuple[RootInterval, ...]
    distinct_real_roots: int
    total_real_roots_with_multiplicity: int
    degree: int
    factors: tuple[tuple[int, int], ...]  # (multiplicity, factor degree)
    cauchy_bound: int
    chain_length: int
    evaluations: int
    max_depth: int
    notes: tuple[str, ...] = field(default_factory=tuple)


class _BudgetMeter:
    """Counts exact Sturm evaluations so requests cannot run unchecked."""

    def __init__(self, budget: BudgetConfig) -> None:
        self.budget = budget
        self.evaluations = 0
        self.max_depth = 0

    def evaluate(self, chain: list[RationalPoly], x: Fraction) -> list[int]:
        self.evaluations += len(chain)
        if self.evaluations > self.budget.max_sturm_pairs:
            raise IsolationError(
                ErrorCategory.BUDGET_EXCEEDED,
                f"Sturm evaluation budget exhausted: more than "
                f"{self.budget.max_sturm_pairs} chain-member evaluations",
                state={"evaluations": self.evaluations, "limit": self.budget.max_sturm_pairs},
            )
        return sign_sequence(chain, x)

    def note_depth(self, depth: int) -> None:
        self.max_depth = max(self.max_depth, depth)
        if depth > self.budget.max_bisection_depth:
            raise IsolationError(
                ErrorCategory.INCONCLUSIVE,
                f"bisection depth {depth} exceeds max_bisection_depth="
                f"{self.budget.max_bisection_depth}; roots may be closer than the "
                "configured limit can separate",
                state={"depth": depth, "limit": self.budget.max_bisection_depth},
            )


def _count_interval(
    chain: list[RationalPoly],
    left: Fraction,
    right: Fraction,
    meter: _BudgetMeter,
) -> tuple[int, list[int], list[int], int, int]:
    """Return ``(root_count, signs_left, signs_right, v_left, v_right)`` for ``(left, right]``."""

    signs_left = meter.evaluate(chain, left)
    signs_right = meter.evaluate(chain, right)
    v_left = variations_from_signs(signs_left)
    v_right = variations_from_signs(signs_right)
    return v_left - v_right, signs_left, signs_right, v_left, v_right


def _isolate_squarefree(
    radical: RationalPoly,
    budget: BudgetConfig,
    meter: _BudgetMeter,
) -> tuple[list[tuple[Fraction, Fraction, int, list[int], list[int], int, int, int]], int]:
    """Bisect the square-free radical.

    Returns tuples ``(a, b, depth, signs_a, signs_b, v_a, v_b, count)`` with
    count 1, plus the chain length for reporting.
    """

    chain = sturm_chain(radical)
    bound = cauchy_integer_bound(radical)
    left0, right0 = -bound, bound
    count0, _, _, _, _ = _count_interval(chain, left0, right0, meter)
    # Cauchy's bound strictly contains every (real or complex) root. Sturm only
    # counts the real ones, so count0 may be smaller than the degree (e.g.
    # x^2 + 1 gives 0); count0 == 0 simply yields no isolating intervals.

    pending: list[tuple[Fraction, Fraction, int, int]] = [(left0, right0, 0, count0)]
    finished: list[
        tuple[Fraction, Fraction, int, list[int], list[int], int, int, int]
    ] = []
    while pending:
        left, right, depth, count = pending.pop()
        meter.note_depth(depth)
        if count == 0:
            continue
        if count == 1:
            _, s_left, s_right, v_left, v_right = _count_interval(
                chain, left, right, meter
            )
            finished.append(
                (left, right, depth, s_left, s_right, v_left, v_right, 1)
            )
            if len(finished) > budget.max_roots:
                raise IsolationError(
                    ErrorCategory.TOO_MANY_ROOTS,
                    f"more than max_roots={budget.max_roots} isolating intervals produced",
                    state={"limit": budget.max_roots},
                )
            continue
        midpoint = (left + right) / 2
        left_count, _, _, _, _ = _count_interval(chain, left, midpoint, meter)
        right_count = count - left_count  # exact: (a,b] partitions at midpoint
        # Push right first so the left subtree is processed first on pop;
        # ordering is irrelevant to correctness but keeps output sorted.
        pending.append((midpoint, right, depth + 1, right_count))
        pending.append((left, midpoint, depth + 1, left_count))
    finished.sort(key=lambda item: (item[0], item[1]))
    return finished, len(chain)


def _multiplicity_for_interval(
    factors: list[tuple[int, RationalPoly]],
    factor_chains: dict[int, list[RationalPoly]],
    left: Fraction,
    right: Fraction,
    meter: _BudgetMeter,
) -> tuple[int, dict[str, Any]]:
    """Determine the multiplicity of the unique radical root in ``(left, right]``."""

    evidence: dict[str, Any] = {"factor_counts": {}}
    owner: int | None = None
    for multiplicity, factor in factors:
        chain = factor_chains[id(factor)]
        count, s_left, s_right, v_left, v_right = _count_interval(
            chain, left, right, meter
        )
        evidence["factor_counts"][str(multiplicity)] = {
            "factor_degree": factor.degree,
            "variations_left": v_left,
            "variations_right": v_right,
            "roots_in_interval": count,
        }
        if count == 1:
            if owner is not None:
                raise IsolationError(
                    ErrorCategory.INTERNAL_ERROR,
                    "two square-free factors own a root in one isolating interval",
                    state={"multiplicities": [owner, multiplicity]},
                )
            owner = multiplicity
        elif count != 0:
            raise IsolationError(
                ErrorCategory.INTERNAL_ERROR,
                "a square-free factor has multiple roots inside one isolating interval",
                state={"multiplicity": multiplicity, "count": count},
            )
    if owner is None:
        raise IsolationError(
            ErrorCategory.INTERNAL_ERROR,
            "no square-free factor owns the isolated radical root",
            state={"interval": [str(left), str(right)]},
        )
    evidence["assigned_multiplicity"] = owner
    return owner, evidence


def isolate_roots(polynomial: RationalPoly, budget: BudgetConfig) -> IsolationResult:
    """Isolate all real roots of an exact rational polynomial."""

    if polynomial.is_zero:
        return IsolationResult(
            kind="zero_polynomial",
            intervals=(),
            distinct_real_roots=0,
            total_real_roots_with_multiplicity=0,
            degree=-1,
            factors=(),
            cauchy_bound=0,
            chain_length=0,
            evaluations=0,
            max_depth=0,
            notes=(
                "the zero polynomial vanishes at every real number and has no "
                "finite root list; it is not reported through the ordinary root "
                "isolation result",
            ),
        )
    if polynomial.degree == 0:
        return IsolationResult(
            kind="constant_nonzero",
            intervals=(),
            distinct_real_roots=0,
            total_real_roots_with_multiplicity=0,
            degree=0,
            factors=(),
            cauchy_bound=1,
            chain_length=1,
            evaluations=0,
            max_depth=0,
            notes=("non-zero constant polynomials have no roots",),
        )

    factors = square_free_factors(polynomial)
    radical_parts = [factor for _, factor in factors]
    radical = radical_parts[0]
    for part in radical_parts[1:]:
        radical = radical * part

    meter = _BudgetMeter(budget)
    raw_intervals, chain_length = _isolate_squarefree(radical, budget, meter)
    factor_chains = {id(factor): sturm_chain(factor) for _, factor in factors}

    intervals: list[RootInterval] = []
    total_with_multiplicity = 0
    for left, right, depth, s_left, s_right, v_left, v_right, count in raw_intervals:
        multiplicity, mult_evidence = _multiplicity_for_interval(
            factors, factor_chains, left, right, meter
        )
        total_with_multiplicity += multiplicity
        proof = IntervalProof(
            signs_left=list(s_left),
            signs_right=list(s_right),
            variations_left=v_left,
            variations_right=v_right,
            distinct_roots=v_left - v_right,
            depth=depth,
            left_endpoint_is_root=s_left[0] == 0,
            right_endpoint_is_root=s_right[0] == 0,
            multiplicity_evidence=mult_evidence,
        )
        intervals.append(RootInterval(left, right, multiplicity, proof))

    return IsolationResult(
        kind="isolated",
        intervals=tuple(intervals),
        distinct_real_roots=len(intervals),
        total_real_roots_with_multiplicity=total_with_multiplicity,
        degree=polynomial.degree,
        factors=tuple((m, f.degree) for m, f in factors),
        cauchy_bound=int(cauchy_integer_bound(polynomial)),
        chain_length=chain_length,
        evaluations=meter.evaluations,
        max_depth=meter.max_depth,
        notes=(),
    )
