"""Randomized property tests over synthetic polynomials with known roots.

Ground truth is known BY CONSTRUCTION (we multiply ``(x-r)^m``), not derived
from the kernel. For every random polynomial we assert:

* distinct/with-multiplicity root counts match the construction;
* every known rational root lies in exactly one half-open interval;
* intervals are pairwise disjoint;
* the exact Sturm, exact Descartes/Vincent and independent sparse Sturm oracle
  all agree on the total count over the Cauchy span;
* the verdict is never ``rejected`` (an exact disagreement) for valid inputs.
"""

from __future__ import annotations

import random
from fractions import Fraction as F

import pytest

from root_isolator.evidence.verifier import build_verdict
from root_isolator.kernel.isolate import isolate_roots
from root_isolator.kernel.polynomial import RationalPoly
from root_isolator.kernel.squarefree import square_free_factors

from . import oracle


def _random_roots(rng: random.Random, count: int) -> list[tuple[F, int]]:
    roots: set[F] = set()
    while len(roots) < count:
        root = F(rng.randint(-5, 5), rng.randint(1, 3))
        roots.add(root)
    return [(root, rng.randint(1, 3)) for root in sorted(roots)]


@pytest.mark.parametrize("seed", range(30))
def test_random_polynomials_end_to_end(seed, budget, numeric):
    rng = random.Random(1000 + seed)
    spec = _random_roots(rng, rng.randint(1, 5))
    sparse = oracle.poly_from_roots(spec)
    poly = RationalPoly(oracle.to_dense_ascending(sparse))

    result = isolate_roots(poly, budget)
    verdict = build_verdict(poly, result, numeric)

    known_distinct = sorted({root for root, _ in spec})
    expected_total_mult = sum(mult for _, mult in spec)

    assert result.distinct_real_roots == len(known_distinct)
    assert result.total_real_roots_with_multiplicity == expected_total_mult

    # Multiplicity of every known root matches its construction multiplicity.
    mult_map = {root: mult for root, mult in spec}
    for root in known_distinct:
        owners = [iv for iv in result.intervals if iv.left < root <= iv.right]
        assert len(owners) == 1
        assert owners[0].multiplicity == mult_map[root]

    # Pairwise disjoint.
    ordered = sorted(result.intervals, key=lambda iv: iv.left)
    for left, right in zip(ordered, ordered[1:]):
        assert left.right <= right.left

    # Independent sparse Sturm oracle agrees on the global count.
    radical_sparse = oracle.poly_from_roots([(r, 1) for r in known_distinct])
    ochain = oracle.sturm_chain_oracle(radical_sparse)
    bound = F(result.cauchy_bound)
    oracle_count = oracle.count_distinct_roots_open_closed(ochain, -bound, bound)
    assert oracle_count == len(known_distinct)

    # Every exact (Sturm + Descartes) check passes; numeric may be indeterminate
    # for genuinely tiny separations but must never contradict exact evidence.
    exact_checks = [
        c for c in verdict.checks if c["name"] != "float64_witness"
    ]
    assert all(c["passed"] for c in exact_checks if c["name"] != "mpmath_witness")
    assert verdict.status in {"accepted", "indeterminate"}
    assert verdict.status != "rejected"


def test_square_free_factors_reconstruct_monic_polynomial():
    rng = random.Random(7)
    spec = _random_roots(rng, 4)
    poly = RationalPoly(oracle.to_dense_ascending(oracle.poly_from_roots(spec)))
    monic = RationalPoly([c / poly.leading() for c in poly.coeffs])
    reconstructed = RationalPoly([F(1)])
    for multiplicity, factor in square_free_factors(poly):
        reconstructed = reconstructed * (factor ** multiplicity)
    assert reconstructed == monic
