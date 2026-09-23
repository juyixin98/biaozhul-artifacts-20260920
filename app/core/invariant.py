"""Two-asset stable-pool invariant and its bounded numerical solvers.

Invariant (normalized units, ``x, y > 0``)
-------------------------------------------

For two assets with normalized reserves ``x`` and ``y``, amplification
parameter ``A``, the invariant (self-defined stable-swap-family curve) is

    D**3/(4*x*y) = 2*A*(x + y) + D*(1 - 2*A)

or, as a scalar root in ``D``::

    f(D) = D**3/(4*x*y) + (2*A - 1)*D - 2*A*(x + y) = 0

* constant product (``x*y`` constant) is the ``A -> 0`` limit of this
  parameterization; the accepted integer range is ``A in [1, 10**5]``, which
  lies entirely in the stable-coin regime -- larger ``A`` flattens the curve
  toward constant-sum around balance.
* at balance ``x = y`` -> ``D = x + y`` exactly for every ``A``.

For a swap, ``D`` is held constant.  Given the new *input-side* reserve
``x'``, the new *output-side* reserve ``y'`` is found by solving the same
invariant.  Multiplying ``D^3/(4*x'*y') = 2*A*(x'+y') + D*(1 - 2*A)``
through by ``4*x'*y'`` gives the quadratic::

    g(y) = 8*A*x'*y**2 + [8*A*x'**2 + 4*x'*D*(1 - 2*A)]*y - D**3 = 0

with economically valid root ``0 < y' < D`` (at ``x' = y' = D/2`` the
expression is exactly 0).  Output is ``y - y'`` (fee effects are applied
separately in :mod:`app.core.quotes`).

Solvers
-------
* :func:`solve_d` / :func:`solve_y`: a **safeguarded (bracketed) Newton**
  iterator -- the production solver.  Every Newton step is confined to a
  sign-bracketed interval; a step that lands outside the bracket is replaced
  by the bisection point, so divergence is impossible and convergence is
  bounded.  Iteration count, convergence flag and residual are returned.
* :func:`solve_d_bisect` / :func:`solve_y_bisect`: an **independent pure
  bisection** reference implementation, used only to cross-check the Newton
  root.  It shares no code paths with the primary solver.

Nothing here ever returns a float; all arithmetic is ``Decimal``.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass

from . import config
from .precision import d


class InvariantError(Exception):
    """Raised on inputs outside the domain of the invariant (e.g. zero reserve)."""


@dataclass(frozen=True)
class RootResult:
    """Outcome of one bounded root solve."""

    value: decimal.Decimal
    iterations: int
    converged: bool
    #: dimensionless residual (|F| relative to a characteristic scale)
    residual_rel: decimal.Decimal
    #: number of iterations where Newton had to fall back to a bisection step
    bisection_fallbacks: int
    max_iterations: int

    def as_report(self) -> dict:
        return {
            "value_normalized": str(self.value),
            "iterations": self.iterations,
            "converged": self.converged,
            "residual_rel": format(self.residual_rel, "E"),
            "bisection_fallbacks": self.bisection_fallbacks,
            "max_iterations": self.max_iterations,
        }


# ---------------------------------------------------------------------------
# Residual functions
# ---------------------------------------------------------------------------

def f_d(D: decimal.Decimal, x: decimal.Decimal, y: decimal.Decimal, A: int) -> decimal.Decimal:
    """f(D) for the invariant; root in D."""
    if x <= 0 or y <= 0:
        raise InvariantError("reserves must both be positive to evaluate f(D)")
    a = d(A)
    return D ** 3 / (d(4) * x * y) + (d(2) * a - 1) * D - d(2) * a * (x + y)


def f_d_prime(D: decimal.Decimal, x: decimal.Decimal, y: decimal.Decimal, A: int) -> decimal.Decimal:
    return d(3) * D ** 2 / (d(4) * x * y) + d(2) * A - 1


def g_y(y: decimal.Decimal, x_new: decimal.Decimal, D: decimal.Decimal, A: int) -> decimal.Decimal:
    """g(y): fixed-D swap equation as a quadratic; strictly increasing in y>0.

    ``8*A*x*y^2 + (8*A*x^2 + 4*x*D*(1 - 2*A))*y - D^3``.
    """
    a = d(A)
    return (
        d(8) * a * x_new * y ** 2
        + (d(8) * a * x_new ** 2 + d(4) * x_new * D * (1 - d(2) * a)) * y
        - D ** 3
    )


def g_y_prime(y: decimal.Decimal, x_new: decimal.Decimal, D: decimal.Decimal, A: int) -> decimal.Decimal:
    a = d(A)
    return (
        d(16) * a * x_new * y
        + d(8) * a * x_new ** 2
        + d(4) * x_new * D * (1 - d(2) * a)
    )


# ---------------------------------------------------------------------------
# Generic bounded bracketed-Newton driver
# ---------------------------------------------------------------------------

def _bracketed_newton(
    f,
    fp,
    lo: decimal.Decimal,
    hi: decimal.Decimal,
    initial: decimal.Decimal,
    max_iter: int,
    rel_tol: decimal.Decimal,
    abs_tol: decimal.Decimal,
    characteristic: decimal.Decimal,
    scale: decimal.Decimal,
) -> RootResult:
    """Bounded safeguarded Newton.

    Precondition: f is continuous, strictly increasing and convex on
    ``[lo, hi]`` with ``f(lo) < 0 < f(hi)``.  Strategy:

    * The bracket ``[lo, hi]`` is a *guard*, kept loose.  Each iteration first
      takes the raw Newton step ``cur - f(cur)/fp(cur)``.  For a convex
      increasing function approached from above this step moves monotonically
      toward the root and never undershoots it, so it stays bounded.
    * Only if the raw step lands outside the bracket, equals the current
      point, or fails to reduce ``|f|`` do we fall back to the bisection
      midpoint and tighten the bracket to it.  That fallback both bounds the
      iteration count and guarantees progress even if the convexity argument
      is violated by rounding far from the root.

    Convergence (all required): relative/absolute step below tolerance AND
    relative residual ``|f|/characteristic`` below ``rel_tol``.
    """
    if f(lo) >= 0 or f(hi) <= 0:
        # Caller bug, not a user error.
        raise InvariantError("bracket does not enclose a sign change")

    cur = max(lo, min(hi, initial))
    fallbacks = 0

    for k in range(1, max_iter + 1):
        fx = f(cur)
        rel_res = abs(fx) / max(characteristic, d(1))
        slope = fp(cur)

        if k > 1:
            step = abs(cur - prev)
            step_ok = step < abs_tol or step / max(scale, abs_tol) < rel_tol
            if step_ok and rel_res < rel_tol:
                return RootResult(cur, k, True, rel_res, fallbacks, max_iter)

        nxt = cur - fx / slope
        used_bisect = False

        # Accept the raw Newton step whenever it stays strictly inside the
        # bracket and moves.  For a convex increasing function approached
        # from above it decreases monotonically toward the root and cannot
        # undershoot, so it remains bounded without extra damping.  A step
        # outside the bracket or a stalled step triggers a bisection step.
        if not (lo < nxt < hi) or nxt == cur:
            nxt = (lo + hi) / d(2)
            used_bisect = True
            fallbacks += 1

        fn = f(nxt)
        if fn == 0:
            return RootResult(nxt, k, True, abs(fn) / max(characteristic, d(1)), fallbacks, max_iter)

        if fn > 0:
            # root lies left of nxt; nxt is a safe new upper bound
            if nxt < hi:
                hi = nxt
        else:
            if nxt > lo:
                lo = nxt

        prev, cur = cur, nxt

    # Cap reached: not converged.  Report honest state.
    fx = f(cur)
    rel_res = abs(fx) / max(characteristic, d(1))
    return RootResult(cur, max_iter, False, rel_res, fallbacks, max_iter)


# ---------------------------------------------------------------------------
# D solver
# ---------------------------------------------------------------------------

def _d_bracket(x: decimal.Decimal, y: decimal.Decimal, A: int):
    """f(0) < 0; grow hi until f(hi) > 0."""
    lo = d(0)
    hi = max(d(1), x + y)
    # Bounded growth to avoid pathological infinite loops; with the allowed
    # inputs f grows as D**3 so very few doublings ever occur.
    for _ in range(2000):
        if f_d(hi, x, y, A) > 0:
            return lo, hi
        hi *= 2
    raise InvariantError("could not bracket D")


def _d_characteristic(x: decimal.Decimal, y: decimal.Decimal, A: int) -> decimal.Decimal:
    # Characteristic magnitude of the terms of f(D); used to normalize the
    # residual into a dimensionless number.
    return max(abs(d(2) * A * (x + y)), d(1))


def solve_d(
    x: decimal.Decimal,
    y: decimal.Decimal,
    A: int,
    max_iter: int = config.NEWTON_MAX_ITER_DEFAULT,
) -> RootResult:
    """Safeguarded Newton solve for D given reserves x, y."""
    lo, hi = _d_bracket(x, y, A)
    initial = x + y
    scale = max(x + y, config.ROOT_ABS_TOL)
    return _bracketed_newton(
        lambda D: f_d(D, x, y, A),
        lambda D: f_d_prime(D, x, y, A),
        lo,
        hi,
        initial,
        max_iter,
        config.NEWTON_REL_TOL,
        config.ROOT_ABS_TOL,
        _d_characteristic(x, y, A),
        scale,
    )


def solve_d_bisect(
    x: decimal.Decimal,
    y: decimal.Decimal,
    A: int,
    max_iter: int = config.BISECT_MAX_ITER,
    abs_tol: decimal.Decimal = config.BISECT_ABS_TOL,
) -> RootResult:
    """Independent pure-bisection reference solve for D."""
    lo, hi = _d_bracket(x, y, A)
    two = d(2)
    mid = hi
    char = _d_characteristic(x, y, A)
    for k in range(1, max_iter + 1):
        mid = (lo + hi) / two
        fm = f_d(mid, x, y, A)
        if fm == 0 or (hi - lo) < abs_tol:
            return RootResult(mid, k, True, abs(fm) / char, 0, max_iter)
        if fm > 0:
            hi = mid
        else:
            lo = mid
    fm = f_d(mid, x, y, A)
    return RootResult(mid, max_iter, False, abs(fm) / char, 0, max_iter)


# ---------------------------------------------------------------------------
# y solver (swap equation)
# ---------------------------------------------------------------------------

def _y_bracket_full(x_new: decimal.Decimal, D: decimal.Decimal, A: int) -> decimal.Decimal:
    """Return ``hi`` with g(hi) >= 0 for a bracket beginning at ``lo = 0``.

    The upper bound starts at ``D`` (the economically valid root always lies
    in ``(0, D)`` for a feasible buy) and is grown only for the pathological
    request where even y' = D does not supply enough output.  The bisection
    *reference* solver always uses this full bracket, so it cannot share a
    too-large upper bound with Newton and silently miss a microscopic root.
    """
    hi = D
    for _ in range(2000):
        if g_y(hi, x_new, D, A) >= 0:
            return hi
        hi *= 2
    raise InvariantError("could not bracket y'")


def _y_bracket_with_hint(
    x_new: decimal.Decimal, D: decimal.Decimal, A: int, y_prev: decimal.Decimal
) -> decimal.Decimal:
    """Tight upper bound for Newton: the pre-swap reserve when feasible."""
    if y_prev > 0 and g_y(y_prev, x_new, D, A) >= 0:
        return y_prev
    return _y_bracket_full(x_new, D, A)


def _y_characteristic(x_new: decimal.Decimal, D: decimal.Decimal, A: int) -> decimal.Decimal:
    # Magnitude of the terms of g at y ~ D.
    a = d(A)
    return max(
        d(8) * a * x_new * D ** 2,
        d(8) * a * x_new ** 2 * D,
        d(4) * x_new * D ** 2 * abs(1 - d(2) * a),
        D ** 3,
        d(1),
    )


def solve_y(
    x_new: decimal.Decimal,
    D: decimal.Decimal,
    A: int,
    y_prev: decimal.Decimal,
    max_iter: int = config.NEWTON_MAX_ITER_DEFAULT,
) -> RootResult:
    """Safeguarded Newton solve for the post-swap output reserve y'.

    Newton is bracketed by the tight interval ``(0, y_prev)`` and seeded at
    ``y_prev`` with a relative contract; it is efficient for ordinary trades.
    The independent :func:`solve_y_bisect` uses the full ``(0, D)`` bracket
    with an absolute contract and must agree, so a microscopic output that
    Newton could miss is always caught by the cross-check.
    """
    if x_new <= 0 or D <= 0 or y_prev <= 0:
        raise InvariantError("x', D and y_prev must be positive")
    lo = d(0)
    hi = _y_bracket_with_hint(x_new, D, A, y_prev)
    initial = hi  # start at pre-trade reserve
    scale = max(hi, config.ROOT_ABS_TOL)
    return _bracketed_newton(
        lambda yy: g_y(yy, x_new, D, A),
        lambda yy: g_y_prime(yy, x_new, D, A),
        lo,
        hi,
        initial,
        max_iter,
        config.NEWTON_REL_TOL,
        config.ROOT_ABS_TOL,
        _y_characteristic(x_new, D, A),
        scale,
    )


def solve_y_bisect(
    x_new: decimal.Decimal,
    D: decimal.Decimal,
    A: int,
    max_iter: int = config.BISECT_MAX_ITER,
    abs_tol: decimal.Decimal = config.BISECT_ABS_TOL,
) -> RootResult:
    """Independent pure-bisection reference solve for y'.

    Deliberately uses the full bracket ``(0, hi>=D)`` and an absolute-width
    stopping rule, independent of Newton's tight/relative path -- so the two
    solvers cannot make the same missing-a-microscopic-root mistake.
    """
    lo = d(0)
    hi = _y_bracket_full(x_new, D, A)
    two = d(2)
    mid = hi
    char = _y_characteristic(x_new, D, A)
    for k in range(1, max_iter + 1):
        mid = (lo + hi) / two
        gm = g_y(mid, x_new, D, A)
        if gm == 0 or (hi - lo) < abs_tol:
            return RootResult(mid, k, True, abs(gm) / char, 0, max_iter)
        if gm > 0:
            hi = mid
        else:
            lo = mid
    gm = g_y(mid, x_new, D, A)
    return RootResult(mid, max_iter, False, abs(gm) / char, 0, max_iter)
