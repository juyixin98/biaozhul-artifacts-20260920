"""Kernel tests against an independent mpmath high-precision reference."""
import numpy as np
import pytest
import scipy.sparse as sp

from krylov_expm.config import SolverConfig
from krylov_expm.propagator import propagate

from conftest import load_fixture
from reference import reference_expmv, relative_error

TIGHT = SolverConfig(default_tol=1e-10)


@pytest.mark.parametrize(
    "name,t",
    [
        ("symmetric_tridiag", 0.7),   # normal, well-conditioned
        ("grcar_nonnormal", 1.3),     # highly non-normal
        ("jordan_block", 2.0),        # defective (Jordan block)
        ("decay_longtime", 25.0),     # long time, forces segmentation
        ("grcar_nonnormal", -0.9),    # negative t (backward propagation)
    ],
)
def test_matches_high_precision_reference(name, t):
    A, v = load_fixture(name)
    w, evidence = propagate(A, v, t, tol=1e-10, config=TIGHT)
    w_ref = reference_expmv(A.toarray(), t, v)
    assert relative_error(w, w_ref) < 1e-7
    assert evidence.cumulative_error_estimate >= 0.0
    assert evidence.total_matvec_count > 0


def test_happy_breakdown_residual_is_exactly_zero():
    # A e1 = 0.5 e1 for the Jordan fixture, so Arnoldi breaks down at dim 1
    # and the approximation exp(tA)e1 = exp(0.5 t) e1 is exact.
    A, _ = load_fixture("jordan_block")
    e1 = np.zeros(A.shape[0])
    e1[0] = 1.0
    w, evidence = propagate(A, e1, 1.7, tol=1e-12, config=TIGHT)
    expected = np.exp(0.5 * 1.7) * e1
    assert np.allclose(w, expected, rtol=1e-10, atol=1e-13)
    assert all(seg.subspace_residual_norm == 0.0 for seg in evidence.segments)
    assert all(seg.krylov_dim == 1 for seg in evidence.segments)


def test_zero_matrix_returns_input_vector():
    A = sp.csr_matrix((5, 5))
    v = np.arange(1.0, 6.0)
    w, evidence = propagate(A, v, 3.0, tol=1e-8, config=SolverConfig())
    assert np.array_equal(w, v)
    assert evidence.cumulative_error_estimate == 0.0


def test_restart_split_recovers_convergence():
    # max_krylov_dim too small for one step of t=1.0: the fixed restart rule
    # must halve the step and still meet the tolerance.
    A, v = load_fixture("decay_longtime")
    cfg = SolverConfig(max_krylov_dim=8, segment_theta=1.0e9, max_restart_splits=6)
    w, evidence = propagate(A, v, 1.0, tol=1e-8, config=cfg)
    seg = evidence.segments[0]
    assert 1 <= seg.restart_splits <= cfg.max_restart_splits
    assert seg.converged
    w_ref = reference_expmv(A.toarray(), 1.0, v)
    assert relative_error(w, w_ref) < 1e-7


def test_residual_and_error_estimate_are_distinct_fields():
    # max_krylov_dim < n avoids happy breakdown, so both quantities are
    # nonzero and generically different.
    A, v = load_fixture("decay_longtime")
    cfg = SolverConfig(max_krylov_dim=6, segment_theta=1.0e9)
    _, evidence = propagate(A, v, 0.1, tol=1e-6, config=cfg)
    seg = evidence.segments[0]
    assert seg.krylov_dim == 6
    assert seg.subspace_residual_norm > 0.0
    assert seg.error_estimate > 0.0
    # The two quantities measure different things and differ generically.
    assert seg.subspace_residual_norm != seg.error_estimate
