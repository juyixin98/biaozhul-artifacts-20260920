"""Test-only independent oracles.

Nothing here imports the kernel under test. It provides two answers generated
by completely different mechanisms, so the test suite cannot pass merely by
agreeing with itself:

* :func:`reference_sturm_count` — an independently written Sturm
  implementation (integer pseudo-remainder chain with its own polynomial
  routines) used to audit sign-variation counts.
* :func:`mpmath_real_roots` — ``mpmath.polyroots`` at high precision, a global
  numerical root finder unrelated to bisection, used for root locations and
  counts.
"""
from __future__ import annotations

from fractions import Fraction

import mpmath

# ---------------------------------------------------------------------------
# Independent integer-polynomial representation
# ---------------------------------------------------------------------------

IntPoly = tuple[int, ...]  # ascending powers, primitive integers, trimmed


def _itrim(q: list[int]) -> IntPoly:
    end = len(q)
    while end and q[end - 1] == 0:
        end -= 1
    return tuple(q[:end])


def _iscale(q: IntPoly, k: int) -> IntPoly:
    return _itrim([k * c for c in q]) if k else ()


def _iadd(a: IntPoly, b: IntPoly) -> IntPoly:
    out = [0] * max(len(a), len(b))
    for i, c in enumerate(a):
        out[i] += c
    for i, c in enumerate(b):
        out[i] += c
    return _itrim(out)


def _imul(a: IntPoly, b: IntPoly) -> IntPoly:
    if not a or not b:
        return ()
    out = [0] * (len(a) + len(b) - 1)
    for i, ci in enumerate(a):
        for j, cj in enumerate(b):
            out[i + j] += ci * cj
    return _itrim(out)


def _ideriv(a: IntPoly) -> IntPoly:
    return _itrim([i * a[i] for i in range(1, len(a))])


def _ineg(a: IntPoly) -> IntPoly:
    return tuple(-c for c in a) if a else ()


def _isign(a: IntPoly, x: Fraction) -> int:
    """Evaluate an integer polynomial at a rational point exactly."""
    num, den = x.numerator, x.denominator
    # value = sum a_i num^i / den^i; accumulate as a single fraction.
    value = Fraction(0)
    dn = Fraction(num, den)
    for c in reversed(a):
        value = value * dn + Fraction(c)
    return (value > 0) - (value < 0)


def _prem(a: IntPoly, b: IntPoly) -> IntPoly:
    """Pseudo-remainder of integer polynomials a, b.

    Classical pseudo-division multiplies the remainder row by ``lc(b)`` before
    each leading-term cancellation, so after ``t`` elimination steps the
    result equals ``lc(b)**t * rem(a, b)`` with ``t = deg(a)-deg(b)+1``.
    Sturm needs the sign of the true remainder (only positive scalar factors
    are harmless), so the accumulated multiplier sign is corrected: when
    ``lc(b)`` is negative and ``t`` is odd the result is negated.
    """
    r: list[int] = list(a)
    db = len(b) - 1
    lb = b[-1]
    steps = 0
    while r and len(r) - 1 >= db:
        shift = (len(r) - 1) - db
        factor = r[-1]              # leading coefficient before scaling
        r = [lb * c for c in r]     # multiply whole row by lc(b)
        for j in range(len(b)):
            r[shift + j] -= factor * b[j]
        r = list(_itrim(r))
        steps += 1
    if lb < 0 and steps % 2 == 1:
        r = [-c for c in r]
    return tuple(r)


def reference_sturm_chain(q: IntPoly) -> list[IntPoly]:
    """Sturm chain via integer pseudo-remainders (independent reference)."""
    chain = [q, _ideriv(q)]
    while chain[-1]:
        nxt = _ineg(_prem(chain[-2], chain[-1]))
        if not nxt:
            return chain
        chain.append(nxt)
    return chain


def _variations(signs: list[int]) -> int:
    kept = [s for s in signs if s]
    return sum(1 for i in range(len(kept) - 1) if kept[i] != kept[i + 1])


def reference_sturm_count(q: IntPoly, a: Fraction, b: Fraction) -> int:
    """Distinct real roots of ``q`` in the half-open cell (a, b].

    Zeros at a point are skipped — the same Sturm convention the kernel uses,
    applied to an independently constructed chain.
    """
    chain = reference_sturm_chain(q)
    va = _variations([_isign(m, a) for m in chain])
    vb = _variations([_isign(m, b) for m in chain])
    return va - vb


def reference_total_real_roots(q: IntPoly) -> int:
    """Distinct real roots on the whole line, counting from -inf/+inf signs."""
    chain = reference_sturm_chain(q)
    v_inf = _variations([(1 if m[-1] > 0 else -1) for m in chain])
    v_minf = _variations(
        [(1 if (m[-1] * ((-1) ** (len(m) - 1))) > 0 else -1) for m in chain]
    )
    return v_minf - v_inf


# ---------------------------------------------------------------------------
# mpmath high-precision global oracle
# ---------------------------------------------------------------------------

def mpmath_real_roots(fraction_coeffs: tuple[Fraction, ...], dps: int = 80,
                      tol: float = 1e-50) -> tuple[mpmath.mpf, ...]:
    """Real roots from ``mpmath.polyroots`` — independent global solver.

    Coefficients are ascending-power exact rationals, converted to mpmath at
    ``dps``. Roots whose imaginary part is below ``tol`` are returned sorted
    as real mpf values.
    """
    mpmath.mp.dps = dps
    ascending = [mpmath.mpf(c.numerator) / mpmath.mpf(c.denominator)
                 for c in fraction_coeffs]
    roots = mpmath.polyroots(ascending, maxsteps=200, extraprec=200, asc=True)
    real = sorted(
        r.real for r in roots
        if abs(r.imag) < mpmath.mpf(tol)
    )
    # Merge duplicates that are numerical artifacts of repeated roots.
    merged: list[mpmath.mpf] = []
    for value in real:
        if not merged or abs(value - merged[-1]) > mpmath.mpf(10) ** (-dps // 3):
            merged.append(value)
    return tuple(merged)
