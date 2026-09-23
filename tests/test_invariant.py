"""Tests for the invariant and the two independent root solvers.

These exercise the math directly (no HTTP) under the 80-digit Decimal context.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from app.core import config, invariant as inv
from tests.conftest import ABS_TIGHT, REL_TIGHT, dec


# ---------------------------------------------------------------------------
# Invariant identities
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("A", [1, 2, 5, 20, 100, 10_000, 100_000])
def test_balance_D_equals_sum(hp, A):
    """At x=y, D = 2x for every A (both the A*S and the product term agree)."""
    for scale in ["1", "100", "100000000000", "1E30"]:
        x = y = dec(scale)
        r = inv.solve_d(x, y, A)
        assert r.converged
        assert r.value == x + y
        # f(D) must vanish.
        assert abs(inv.f_d(r.value, x, y, A)) <= D("1E-60")


def test_constant_product_limit_A_zero(hp):
    """Constant product is the A -> 0 limit of this parameterization.

    f(D) = D^3/(4xy) + (2A-1)D - 2A(x+y); at A=0 this is D^3/(4xy) - D = 0,
    whose positive root is D = 2 sqrt(xy) -- the constant-product relation
    x*y = (D/2)^2.  (The accepted API range is integer A >= 1, i.e. the
    stable-coin regime; this is a math-only check of the limit.)
    """
    x, y = dec("123.456"), dec("7.89")
    root = 2 * (x * y).sqrt()
    f = root ** 3 / (4 * x * y) - root
    assert abs(f) < D("1E-70")


def test_g_quadratic_zero_at_balance(hp):
    """g(y') must be exactly 0 at x'=y'=D/2 for all A."""
    for A in [1, 20, 100_000]:
        x = y = dec("100")
        Dv = inv.solve_d(x, y, A).value
        assert inv.g_y(y, x, Dv, A) == 0


def test_f_and_g_solutions_satisfy_residuals(hp):
    """Converged roots genuinely zero their residuals (not just stopped)."""
    x, y, A = dec("100"), dec("100"), 20
    Dv = inv.solve_d(x, y, A).value
    x_new = dec("110")
    yp = inv.solve_y(x_new, Dv, A, y_prev=y).value
    assert abs(inv.g_y(yp, x_new, Dv, A)) / Dv ** 3 < D("1E-70")
    assert 0 < yp < Dv


# ---------------------------------------------------------------------------
# Newton vs independent bisection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("A", [1, 20, 100_000])
@pytest.mark.parametrize("R", [1, 10, 1_000, 10 ** 6, 10 ** 12, 10 ** 18, 10 ** 30])
def test_D_newton_matches_bisection_extreme_imbalance(hp, A, R):
    x = dec("100")
    y = dec("100") / R
    rn = inv.solve_d(x, y, A, max_iter=config.NEWTON_MAX_ITER_HARD_CAP)
    rb = inv.solve_d_bisect(x, y, A)
    assert rn.converged and rb.converged
    assert abs(rn.value - rb.value) / max(rn.value, rb.value) < REL_TIGHT
    # independent residual cross-check
    assert abs(inv.f_d(rn.value, x, y, A)) < D("1E-40") * max(x + y, D(1))


@pytest.mark.parametrize("eps", ["1E-6", "1E-10", "1E-30", "1E-60"])
def test_tiny_trades_resolved(hp, eps):
    """A microscopic input must produce a microscopic output, not be lost."""
    x = y = dec("100")
    A = 100
    Dv = inv.solve_d(x, y, A).value
    x_new = x + dec(eps)
    rn = inv.solve_y(x_new, Dv, A, y_prev=y)
    rb = inv.solve_y_bisect(x_new, Dv, A)
    assert rn.converged and rb.converged
    out = y - rn.value
    # near balance, stable-swap gives out ~ eps; require it within [eps/2,eps]
    assert dec(eps) / 2 < out <= dec(eps)
    # absolute agreement between the two solvers must catch a missed micro-root
    assert abs(rn.value - rb.value) < D("1E-50")


def test_solvers_are_independent_paths(hp):
    """Newton uses a tight (0,y_prev) relative path; bisection full (0,D>=).

    A deliberately wrong Newton root seeded far away cannot be masked by the
    bisection solver because bisection rebuilds its own bracket from 0.
    """
    x = y = dec("100")
    A = 20
    Dv = inv.solve_d(x, y, A).value
    # y_prev hint that is already above D (pathological): Newton grows bracket
    rn = inv.solve_y(dec("100.0001"), Dv, A, y_prev=dec("100"))
    rb = inv.solve_y_bisect(dec("100.0001"), Dv, A)
    assert rn.converged and rb.converged
    assert abs(rn.value - rb.value) < ABS_TIGHT * 10


# ---------------------------------------------------------------------------
# Bounded iteration / iteration cap
# ---------------------------------------------------------------------------

def test_iteration_cap_D_reports_nonconvergence(hp):
    x = dec("100")
    y = dec("100") / (10 ** 18)
    r = inv.solve_d(x, y, 20, max_iter=5)
    assert not r.converged
    assert r.iterations == 5
    assert r.residual_rel > 0


def test_iteration_cap_y_reports_nonconvergence(hp):
    x = dec("100")
    y = dec("100") / (10 ** 18)
    Dv = inv.solve_d(x, y, 20, max_iter=config.NEWTON_MAX_ITER_HARD_CAP).value
    r = inv.solve_y(x + dec("1E-12"), Dv, 20, y_prev=y, max_iter=1)
    assert not r.converged
    assert r.iterations == 1


def test_newton_cannot_diverge(hp):
    """Even from the far edge the safeguarded iterate stays bracketed."""
    x = dec("100")
    y = dec("100") / (10 ** 30)
    r = inv.solve_d(x, y, 20, max_iter=config.NEWTON_MAX_ITER_HARD_CAP)
    rb = inv.solve_d_bisect(x, y, 20)
    assert r.converged
    # result must lie inside the global feasible order of magnitude
    assert 0 < r.value <= x + y
    assert abs(r.value - rb.value) / rb.value < REL_TIGHT


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------

def test_zero_reserve_raises(hp):
    with pytest.raises(inv.InvariantError):
        inv.solve_d(dec("0"), dec("100"), 20)
    with pytest.raises(inv.InvariantError):
        inv.solve_d(dec("100"), dec("0"), 20)


def test_y_nonpositive_arguments_raise(hp):
    with pytest.raises(inv.InvariantError):
        inv.solve_y(dec("0"), dec("200"), 20, y_prev=dec("100"))
    with pytest.raises(inv.InvariantError):
        inv.solve_y(dec("100"), dec("200"), 20, y_prev=dec("0"))


def test_infeasible_huge_input_gross_output_capped_below_reserve(hp):
    """An astronomically large input cannot withdraw more than the reserve.

    The root y' approaches 0 from above, so gross output approaches (but
    never reaches) the full reserve; the net-after-fee integer payout is
    always strictly below the reserve, so the post-trade invariant check
    stays the authority on feasibility.
    """
    x = y = dec("100")
    A = 20
    Dv = inv.solve_d(x, y, A).value
    rn = inv.solve_y(dec("1E12"), Dv, A, y_prev=y)
    assert rn.converged
    assert 0 < rn.value < y
    gross = y - rn.value
    assert 0 < gross < y  # never the whole reserve, let alone more
