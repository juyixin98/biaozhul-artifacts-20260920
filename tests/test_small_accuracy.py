"""Accuracy against the independent mpmath reference for small matrices."""

from __future__ import annotations

import numpy as np
import pytest

from krylov_expv.core.integrator import expv

from .conftest import load_fixture
from .reference import reference_expv, relative_error


@pytest.mark.parametrize("name", ["diag3", "jordan4", "nonnormal8", "rotation3"])
def test_small_matrices_match_mpmath_reference(name):
    payload, A, v = load_fixture(name)
    result = expv(A, payload["t"], v, tol=payload["tol"])
    ref = reference_expv(A, payload["t"], v)

    assert result.converged, result.evidence.failure_message
    assert relative_error(result.w, ref) < 100 * payload["tol"]
    # the reported estimate must be a plausible bound on the actual error:
    # not absurdly small, not absurdly large.  A zero estimate is only
    # legitimate after a happy breakdown, where the answer is exact up to
    # roundoff.
    actual = relative_error(result.w, ref)
    estimate = result.evidence.total_error_estimate / max(float(np.linalg.norm(ref)), 1e-300)
    if estimate == 0.0:
        assert all(s.happy_breakdown for s in result.evidence.steps)
        assert actual < 1e-12
    else:
        assert estimate >= 0.01 * actual
        assert estimate <= 1e6 * max(actual, 1e-16)


def test_diag3_closed_form():
    payload, A, v = load_fixture("diag3")
    import numpy as np

    t = payload["t"]
    result = expv(A, t, v, tol=payload["tol"])
    expected = v * np.exp(t * np.array([-1.0, -0.5, 0.25]))
    assert np.allclose(result.w, expected, rtol=1e-11, atol=1e-14)
