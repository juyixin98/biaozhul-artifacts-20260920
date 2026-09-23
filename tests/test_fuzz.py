"""Deterministic randomized cross-checks (seeded; no external deps).

Property-style: for random pools/inputs the Newton and bisection roots must
agree, the invariant residual must vanish, and D must be conserved by the
exact swap (before fee/floor effects).
"""

from __future__ import annotations

import random
from decimal import Decimal as D

import pytest

from app.core import invariant as inv
from tests.conftest import dec

SEED = 20260922
CASES = 60


def _cases():
    rng = random.Random(SEED)
    for _ in range(CASES):
        # log-uniform reserves over 1e-12 .. 1e12
        x = D(10) ** D(rng.uniform(-12, 12))
        y = D(10) ** D(rng.uniform(-12, 12))
        A = rng.choice([1, 2, 5, 20, 100, 1000, 100_000])
        frac = D(10) ** D(rng.uniform(-30, 0))  # input fraction of x
        yield x, y, A, frac


@pytest.mark.parametrize("x,y,A,frac", list(_cases()))
def test_random_pool_solver_agreement(hp, x, y, A, frac):
    rn = inv.solve_d(x, y, A, max_iter=512)
    rb = inv.solve_d_bisect(x, y, A)
    assert rn.converged and rb.converged
    assert abs(rn.value - rb.value) / max(rn.value, rb.value) < D("1E-40")

    Dv = rn.value
    x_new = x + x * frac
    yn = inv.solve_y(x_new, Dv, A, y_prev=y, max_iter=512)
    yb = inv.solve_y_bisect(x_new, Dv, A)
    assert yn.converged and yb.converged
    # absolute agreement catches microscopic outputs
    assert abs(yn.value - yb.value) < D("1E-50")
    # the exact swap conserves D: f(D; x_new, y') == 0
    resid = inv.f_d(Dv, x_new, yn.value, A)
    scale = max(2 * A * (x_new + yn.value), D(1))
    assert abs(resid) / scale < D("1E-40")
    # output direction is sane for a buy
    assert yn.value <= y
