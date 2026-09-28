"""Non-normal matrix: transient growth must be captured accurately."""

from __future__ import annotations

import numpy as np

from krylov_expv.core.integrator import expv

from .conftest import load_fixture
from .reference import reference_expv, relative_error


def test_nonnormal_matrix_matches_reference_despite_transient_growth():
    payload, A, v = load_fixture("nonnormal8")
    t = payload["t"]
    result = expv(A, t, v, tol=payload["tol"])
    ref = reference_expv(A, t, v)

    assert result.converged
    assert relative_error(result.w, ref) < 1e-8
    # sanity: the fixture really is non-normal (A A^T != A^T A)
    dense = A.toarray()
    assert not np.allclose(dense @ dense.T, dense.T @ dense)


def test_error_evidence_distinguishes_residual_from_estimate():
    payload, A, v = load_fixture("nonnormal8")
    # m_max=7 keeps the Krylov subspace proper (n=8), so both error
    # quantities are nonzero and genuinely distinct
    from krylov_expv.config import ExpvConfig

    result = expv(A, payload["t"], v, config=ExpvConfig(m_max=7), tol=payload["tol"])
    assert result.converged
    assert result.evidence.num_steps >= 1
    saw_distinct = False
    for step in result.evidence.steps:
        assert step.subspace_residual >= 0.0
        assert step.error_estimate >= 0.0
        if step.subspace_residual != step.error_estimate:
            saw_distinct = True
    assert saw_distinct, "residual and error estimate must not be the same number"
    assert result.evidence.total_error_estimate > 0.0
    assert result.evidence.max_subspace_residual > 0.0
