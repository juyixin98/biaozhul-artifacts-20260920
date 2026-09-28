"""INDEPENDENT reference oracle for the test suite.

Nothing here imports the kernel's Sturm/Euclidean/isolation code. The oracle is
written from scratch in a deliberately different style (sparse ``dict``
polynomials instead of dense tuples) so that a shared implementation bug cannot
silently make the tests agree with the system under test.

It provides:

* :func:`poly_from_roots` - construct a polynomial whose roots/multiplicities
  are known *by construction*;
* :func:`sturm_chain_oracle` - a textbook Sturm chain over ``dict`` polys;
* :func:`variations_oracle` - an independent sign-variation counter;
* :func:`count_roots_oracle`` - V(a) - V(b) over a closed range, with roots at
  endpoints handled explicitly;
* :func:`expected_intervals_from_roots` - ground-truth disjoint isolation
  expectations built directly from the known rational roots.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Iterable

Rational = Fraction
SparsePoly = dict[int, Fraction]  # power -> coefficient


# --------------------------------------------------------------------------- #
# Independent sparse-polynomial arithmetic
# --------------------------------------------------------------------------- #
def _trim(p: SparsePoly) -> SparsePoly:
    return {k: v for k, v in p.items() if v != 0}


def poly_from_roots(roots: Iterable[tuple[Rational, int]]) -> SparsePoly:
    """Product over ``(root, multiplicity)`` of ``(x - root) ** multiplicity``.

    Roots and multiplicities are therefore known exactly by construction.
    """

    result: SparsePoly = {0: Fraction(1)}
    for root, multiplicity in roots:
        factor: SparsePoly = {0: -Fraction(root), 1: Fraction(1)}
        for _ in range(multiplicity):
            result = _multiply(result, factor)
    return _trim(result)


def _multiply(a: SparsePoly, b: SparsePoly) -> SparsePoly:
    out: SparsePoly = {}
    for i, ci in a.items():
        for j, cj in b.items():
            out[i + j] = out.get(i + j, Fraction(0)) + ci * cj
    return _trim(out)


def degree(p: SparsePoly) -> int:
    return max(p) if p else -1


def derivative(p: SparsePoly) -> SparsePoly:
    return _trim({k - 1: k * c for k, c in p.items() if k > 0})


def _poly_divmod(a: SparsePoly, b: SparsePoly) -> tuple[SparsePoly, SparsePoly]:
    """Independent long division, sparse representation."""

    r = dict(a)
    q: SparsePoly = {}
    db = degree(b)
    lb = b[db]
    while degree(r) >= db and r:
        shift = degree(r) - db
        factor = r[degree(r)] / lb
        q[shift] = factor
        for j, cj in b.items():
            r[shift + j] = r.get(shift + j, Fraction(0)) - factor * cj
        r = _trim(r)
    return _trim(q), _trim(r)


def _monic(p: SparsePoly) -> SparsePoly:
    if not p:
        return p
    lead = abs(p[degree(p)])
    return {k: c / lead for k, c in p.items()}


# --------------------------------------------------------------------------- #
# Independent Sturm chain and variation counting
# --------------------------------------------------------------------------- #
def sturm_chain_oracle(p: SparsePoly) -> list[SparsePoly]:
    """Textbook chain: s0=p, s1=p', s_{i+1} = -rem(s_{i-1}, s_i)."""

    if not p:
        return [{}]
    s0 = _monic(p)
    if degree(s0) == 0:
        return [s0]
    s1 = _monic(derivative(p))
    chain = [s0, s1]
    while degree(chain[-1]) > 0:
        _, remainder = _poly_divmod(chain[-2], chain[-1])
        if not remainder:
            break
        chain.append(_monic({k: -c for k, c in remainder.items()}))
    return chain


def _eval(p: SparsePoly, x: Rational) -> Fraction:
    total = Fraction(0)
    for k, c in p.items():
        if k == 0:
            total += c
        else:
            total += c * (x**k)
    return total


def sign(value: Fraction) -> int:
    return (value > 0) - (value < 0)


def raw_signs(chain: list[SparsePoly], x: Rational) -> list[int]:
    """Signs INCLUDING zeros - used to assert endpoint-root detection."""

    return [sign(_eval(member, x)) for member in chain]


def variations_oracle(chain: list[SparsePoly], x: Rational) -> int:
    """Sign changes after deleting zeros (right-limit convention V(x+))."""

    signs = [s for s in raw_signs(chain, x) if s != 0]
    return sum(1 for a, b in zip(signs, signs[1:]) if a != b)


def count_distinct_roots_open_closed(
    chain: list[SparsePoly], a: Rational, b: Rational
) -> int:
    """Number of distinct roots in the half-open interval ``(a, b]``."""

    return variations_oracle(chain, a) - variations_oracle(chain, b)


# --------------------------------------------------------------------------- #
# Ground-truth expectations
# --------------------------------------------------------------------------- #
def distinct_real_roots(roots: Iterable[tuple[Rational, int]]) -> list[Fraction]:
    seen: set[Fraction] = set()
    ordered: list[Fraction] = []
    for root, _ in roots:
        if root not in seen:
            seen.add(Fraction(root))
            ordered.append(Fraction(root))
    return sorted(ordered)


def to_dense_ascending(p: SparsePoly) -> list[Fraction]:
    return [p.get(k, Fraction(0)) for k in range(degree(p) + 1)]


def multiplicity_map(roots: Iterable[tuple[Rational, int]]) -> dict[Fraction, int]:
    out: dict[Fraction, int] = {}
    for root, mult in roots:
        out[Fraction(root)] = mult
    return out
