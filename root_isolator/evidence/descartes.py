"""Independent EXACT corroboration via Descartes' rule (Descartes/Vincent).

This module shares no algorithmic code with the Sturm kernel: it counts roots
using a different theorem.

Single-transform Descartes gives an exact answer only when the sign-variation
count ``V`` is 0 or 1. When ``V >= 2`` the rule is merely an upper bound (e.g.
``x^3 + x`` over a wide interval shows 3 variations but has one positive
transformed root), so we *bisect* exactly like Vincent/Collins-Akritas until
every leaf has ``V in {0, 1}``.

Interval convention matches the kernel: the half-open ``(a, b]``. Under the
Möbius map ``x = (b + a t)/(1 + t)`` with denominator cleared,

* ``t = 0``      <-> ``x = b``  (right endpoint, included);
* ``0 < t < inf``<-> ``a < x < b`` (interior);
* ``t -> inf``   <-> ``x -> a`` (left endpoint, excluded).

A root exactly at ``b`` makes the transformed constant term zero; for the
square-free radical that happens with multiplicity exactly one, so it is
counted separately and ``t`` is factored out before reading variations.

The half-open interval partitions exactly as
``(a, b] = (a, m] u (m, b]`` with no double counting (``m`` is included only by
the left child), so recursion needs no endpoint correction.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import comb

from root_isolator.kernel.polynomial import RationalPoly

# Recursion is exact, but a guard bounds pathological cases. An isolating
# interval produced by the kernel reaches V in {0,1} quickly; the guard is far
# beyond any realistic request and turns into "inconclusive" rather than a
# wrong answer if ever hit.
_MAX_DESCARTES_DEPTH = 600


@dataclass(frozen=True)
class DescartesEvidence:
    interval_left: Fraction
    interval_right: Fraction
    root_at_right_endpoint: bool
    descartes_root_count: int
    sign_variations_at_leaf: int
    bisection_nodes: int
    conclusive: bool
    agrees: bool
    reason: str


def _power_row(alpha: Fraction, beta: Fraction, power: int) -> list[Fraction]:
    return [
        comb(power, j) * (alpha ** (power - j)) * (beta**j)
        for j in range(power + 1)
    ]


def _mobius_transform(p: RationalPoly, a: Fraction, b: Fraction) -> RationalPoly:
    """Return ``(1+t)^n p((b + a t)/(1+t))`` exactly (ascending coefficients)."""

    n = p.degree
    if n <= 0:
        return RationalPoly(list(p.coeffs))
    result = [Fraction(0)] * (n + 1)
    for k, ck in p.iter_nonzero():
        left = _power_row(b, a, k)
        right = _power_row(Fraction(1), Fraction(1), n - k)
        for i, li in enumerate(left):
            if li == 0:
                continue
            for j, rj in enumerate(right):
                if rj == 0:
                    continue
                result[i + j] += ck * li * rj
    return RationalPoly(result)


def _variations(coeffs: list[Fraction]) -> int:
    signs = [1 if c > 0 else -1 for c in coeffs if c != 0]
    return sum(1 for x, y in zip(signs, signs[1:]) if x != y)


def count_roots_half_open(
    radical: RationalPoly,
    a: Fraction,
    b: Fraction,
    *,
    depth: int = 0,
    nodes: list[int] | None = None,
) -> tuple[int, bool]:
    """Exact distinct-root count in ``(a, b]``.

    Returns ``(count, conclusive)``. ``conclusive`` is False only if the depth
    guard is reached. The shared ``nodes`` counter records bisection nodes.
    """

    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if depth > _MAX_DESCARTES_DEPTH:
        return 0, False

    g = _mobius_transform(radical, a, b)
    coeffs = list(g.coeffs)

    root_at_b = bool(coeffs) and coeffs[0] == 0  # g(0) = p(b)
    # Square-free radical: a root at b has multiplicity exactly one; factor t.
    interior_coeffs = coeffs[1:] if root_at_b else coeffs
    variations = _variations(interior_coeffs)

    if variations == 0:
        return (1 if root_at_b else 0), True
    if variations == 1:
        return (1 + (1 if root_at_b else 0)), True

    # V >= 2: Descartes is only an upper bound here, so bisect exactly.
    m = (a + b) / 2
    left_count, left_ok = count_roots_half_open(
        radical, a, m, depth=depth + 1, nodes=nodes
    )
    right_count, right_ok = count_roots_half_open(
        radical, m, b, depth=depth + 1, nodes=nodes
    )
    return left_count + right_count, left_ok and right_ok


def descartes_check(
    radical: RationalPoly,
    left: Fraction,
    right: Fraction,
    *,
    expect_root_at_right: bool,
) -> DescartesEvidence:
    """Independently count roots in the isolating ``(left, right]``.

    The kernel claims exactly one distinct root in the interval. The exact
    Descartes/Vincent counter must independently return 1 and agree on whether
    that root sits at the right endpoint.
    """

    exact_root_at_b = radical.eval(right) == 0
    nodes: list[int] = [0]
    count, conclusive = count_roots_half_open(radical, left, right, nodes=nodes)
    nodes_used = nodes[0]
    leaf_variations = -1  # count is exact; per-leaf variations are internal

    def evidence(
        agrees: bool, reason: str, *, conclusive_flag: bool = True
    ) -> DescartesEvidence:
        return DescartesEvidence(
            interval_left=left,
            interval_right=right,
            root_at_right_endpoint=exact_root_at_b,
            descartes_root_count=count,
            sign_variations_at_leaf=leaf_variations,
            bisection_nodes=nodes_used,
            conclusive=conclusive_flag,
            agrees=agrees,
            reason=reason,
        )

    if exact_root_at_b != expect_root_at_right:
        return evidence(
            False,
            f"endpoint disagreement: exact p(b)==0 is {exact_root_at_b} but "
            f"kernel reported right-endpoint root={expect_root_at_right}",
        )

    if not conclusive:
        return evidence(
            False,
            "Descartes/Vincent recursion reached its depth guard before V fell "
            "into {0,1}; cannot judge this interval exactly",
            conclusive_flag=False,
        )

    if count != 1:
        return evidence(
            False,
            "kernel claims 1 distinct root in (a,b], but independent "
            f"Descartes/Vincent counts {count}",
        )

    endpoint_note = (
        "root pinned exactly at the right endpoint"
        if exact_root_at_b
        else "one strictly interior root"
    )
    return evidence(True, f"exact Descartes/Vincent counts 1 root ({endpoint_note})")
