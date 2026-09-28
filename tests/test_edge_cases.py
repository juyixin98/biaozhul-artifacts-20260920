"""Degenerate inputs: t == 0, zero vector, zero matrix, negative t."""

from __future__ import annotations

import numpy as np
import scipy.sparse as sp

from krylov_expv.core.integrator import expv

from .conftest import load_fixture
from .reference import reference_expv, relative_error


def test_t_zero_returns_copy_of_v_without_work():
    payload, A, v = load_fixture("diag3")
    result = expv(A, 0.0, v)
    assert result.converged
    assert result.evidence.termination == "t_zero"
    assert result.evidence.num_steps == 0
    assert np.array_equal(result.w, v)
    assert result.w is not v  # a copy, not an alias


def test_zero_vector_returns_zeros_without_work():
    _, A, _ = load_fixture("jordan4")
    result = expv(A, 3.0, np.zeros(4))
    assert result.converged
    assert result.evidence.termination == "zero_vector"
    assert result.evidence.num_steps == 0
    assert np.array_equal(result.w, np.zeros(4))


def test_zero_matrix_returns_v_unchanged():
    Z = sp.csr_matrix((5, 5))
    v = np.arange(1.0, 6.0)
    result = expv(Z, 2.5, v)
    assert result.converged
    assert np.allclose(result.w, v, atol=1e-14)


def test_negative_t_matches_mpmath_reference():
    payload, A, v = load_fixture("rotation3")
    t = payload["t"]
    assert t < 0.0
    result = expv(A, t, v, tol=payload["tol"])
    ref = reference_expv(A, t, v)
    assert result.converged
    assert relative_error(result.w, ref) < 1e-10
    # step sizes must carry the negative sign: the trace sums back to t
    assert sum(s.tau for s in result.evidence.steps) == t
