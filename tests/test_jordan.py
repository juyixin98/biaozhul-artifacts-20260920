"""Jordan block: defective matrix, Krylov subspace becomes invariant.

For a single 4x4 Jordan block and a generic vector the Krylov subspace
reaches dimension 4 and is then A-invariant: the approximation must be
essentially exact and the residual/estimate must collapse to zero via a
happy breakdown.  This is asserted concretely, not just "no exception".
"""

from __future__ import annotations

import numpy as np

from krylov_expv.core.integrator import expv

from .conftest import load_fixture
from .reference import reference_expv, relative_error


def test_jordan_block_exact_via_happy_breakdown():
    payload, A, v = load_fixture("jordan4")
    result = expv(A, payload["t"], v, tol=payload["tol"])
    ref = reference_expv(A, payload["t"], v)

    assert result.converged
    assert relative_error(result.w, ref) < 1e-11
    # single step, Krylov dimension 4 == size of the Jordan block
    assert result.evidence.num_steps == 1
    step = result.evidence.steps[0]
    assert step.happy_breakdown
    assert step.krylov_dim == 4
    # residual and estimate are both reported and both vanish
    assert step.subspace_residual == 0.0
    assert step.error_estimate == 0.0


def test_jordan_closed_form_first_component():
    # exp(t*J) for J = lambda*I + N has the explicit triangular form; check
    # the last component, which only sees lambda: w_4 = v_4 * exp(lambda t)
    payload, A, v = load_fixture("jordan4")
    t = payload["t"]
    result = expv(A, t, v, tol=payload["tol"])
    lam = -0.7
    assert np.isclose(result.w[3], v[3] * np.exp(lam * t), rtol=1e-11)
