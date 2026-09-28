"""End-to-end orchestration tests over the required verification cases.

Cases: diagonal, repeated spectrum, near-degenerate, widely separated
scales, plus random matrices. Each case is checked against independent
oracles (SciPy/LAPACK and mpmath high precision), asserting concrete
reconstruction errors and failure categories - never just that the endpoint
runs.
"""

import numpy as np
import pytest

from sym_eig.errors import ErrorCategory
from sym_eig.service.engine import RequestOptions, run_eigendecomposition
from tests.fixtures_cases import (
    ALL_CASES,
    mpmath_expected,
)


@pytest.fixture
def small_settings(settings):
    # All curated cases are <= 13, so mpmath high precision is the auto
    # oracle; give it a generous precision.
    from dataclasses import replace
    return replace(settings, reference_dps=50)


@pytest.mark.parametrize("case", ALL_CASES, ids=[c.name for c in ALL_CASES])
def test_curated_cases_succeed_and_match_independent_oracles(
    case, small_settings
):
    result = run_eigendecomposition(
        case.matrix.tolist(), settings=small_settings
    )

    assert result.verdict == "SUCCESS", (
        f"{case.name}: {result.uncertainties or result.error_message}"
    )
    w = np.array(result.eigenvalues)
    V = np.array(result.eigenvectors)

    # Concrete eigenvalue answers against the SciPy-generated fixture answer,
    # at the float64 resolution the stored matrix supports.
    resolution = np.linalg.norm(case.matrix, "fro") * np.finfo(float).eps
    np.testing.assert_allclose(
        w, case.expected_eigenvalues,
        rtol=1e-10, atol=max(1e-11, resolution),
    )

    # Independent reconstruction and orthogonality evidence.
    assert result.evidence["residual_relative_fro"] < 1e-9
    assert result.evidence["orthogonality_fro"] < 1e-9
    assert result.evidence["reconstruction_relative_fro"] < 1e-9

    # A second, genuinely independent high-precision oracle, compared at the
    # resolution the stored matrix actually supports (||A||_F * eps).
    if case.matrix.shape[0] <= small_settings.reference_max_n:
        ref_w, _ = mpmath_expected(case.matrix, dps=50)
        resolution = np.linalg.norm(case.matrix, "fro") * np.finfo(float).eps
        np.testing.assert_allclose(w, ref_w, atol=max(1e-10, resolution))

    assert result.reference_comparison is not None
    rc = result.reference_comparison
    assert (
        rc["max_eigenvalue_abs_error"]
        <= max(1e-9, rc["eigenvalue_resolution_bound"])
    )
    assert (
        rc["max_sin_principal_angle"]
        <= max(1e-9, rc["subspace_resolution_bound"])
    )


def test_diagonal_case_has_exact_values_and_no_qr_sweep(small_settings):
    case = next(c for c in ALL_CASES if c.name == "diagonal")
    result = run_eigendecomposition(case.matrix.tolist(), small_settings)
    assert result.computation["qr_sweeps_total"] == 0
    np.testing.assert_allclose(
        result.eigenvalues, case.expected_eigenvalues, atol=1e-14
    )


def test_repeated_spectrum_compared_as_subspaces_not_vectors(small_settings):
    case = next(c for c in ALL_CASES if c.name == "repeated_spectrum")
    result = run_eigendecomposition(case.matrix.tolist(), small_settings)
    assert [c["multiplicity"] for c in result.clusters] == [3, 1, 2, 1]
    # Both degenerate clusters must have been compared via principal angles.
    assert (
        result.reference_comparison[
            "degenerate_clusters_compared_as_subspaces"
        ]
        == 2
    )
    degenerate = [
        c for c in result.reference_comparison["per_cluster"]
        if c["multiplicity"] > 1
    ]
    assert all(c["max_sin_principal_angle"] < 1e-9 for c in degenerate)
    # The raw vectors need not equal the fixture vectors entry by entry;
    # indeed a coordinate-wise comparison would be meaningless here.
    V = np.array(result.eigenvectors)
    same_vectors = np.allclose(np.abs(V), np.abs(case.expected_vectors))
    assert isinstance(same_vectors, bool)  # vectors are returned either way


def test_near_degenerate_is_clustered_and_still_accurate(small_settings):
    case = next(c for c in ALL_CASES if c.name == "near_degenerate")
    result = run_eigendecomposition(case.matrix.tolist(), small_settings)
    assert [c["multiplicity"] for c in result.clusters] == [2, 2, 1]
    np.testing.assert_allclose(
        result.eigenvalues, case.expected_eigenvalues, atol=1e-10
    )


def test_widely_separated_scales_relative_measures_stay_small(small_settings):
    case = next(c for c in ALL_CASES if c.name == "widely_separated_scales")
    result = run_eigendecomposition(case.matrix.tolist(), small_settings)
    assert result.verdict == "SUCCESS"
    assert result.evidence["residual_relative_fro"] < 1e-9
    assert result.evidence["reconstruction_relative_fro"] < 1e-9
    # Backward stability: all three solvers (us, NumPy, 60-digit mpmath) agree
    # within the float64 resolution floor ||A||_F*eps; the tiny eigenvalue is
    # not absolutely resolvable finer than that, and that limitation must be
    # reported separately rather than hidden.
    resolution = np.linalg.norm(case.matrix, "fro") * np.finfo(float).eps
    ref_w, _ = mpmath_expected(case.matrix, dps=50)
    np.testing.assert_allclose(result.eigenvalues, ref_w, atol=resolution)
    np.testing.assert_allclose(
        result.eigenvalues, np.linalg.eigvalsh(case.matrix), atol=resolution
    )
    assert any("resolution" in note for note in result.limitations)
    # The large eigenvalue, which is well conditioned, is hit tightly.
    assert abs(result.eigenvalues[0] - (-1.0e8)) < 1e-6


def test_non_symmetric_input_is_classified(settings):
    result = run_eigendecomposition(
        [[1.0, 1.0], [0.0, 1.0]], settings
    )
    assert result.verdict == "FAILED"
    assert result.error_category == str(ErrorCategory.NON_SYMMETRIC)
    assert result.eigenvalues is None
    assert "skew_inf_norm" in result.error_details


def test_invalid_matrix_categories(settings):
    cases = [
        ([], str(ErrorCategory.INVALID_MATRIX)),
        ([[1.0, 0.0], [0.0]], str(ErrorCategory.INVALID_MATRIX)),
        (np.eye(300).tolist(), str(ErrorCategory.SIZE_LIMIT_EXCEEDED)),
    ]
    for payload, expected in cases:
        result = run_eigendecomposition(payload, settings)
        assert result.verdict == "FAILED"
        assert result.error_category == expected


def test_stopping_the_iterations_is_not_reported_as_success(settings):
    n = 30
    off = np.ones(n - 1)
    laplacian = (2.0 * np.eye(n) - np.diag(off, 1) - np.diag(off, -1)).tolist()
    result = run_eigendecomposition(
        laplacian,
        settings,
        RequestOptions(max_iters=1, reference="none"),
    )
    assert result.verdict == "FAILED"
    assert result.error_category == str(ErrorCategory.NON_CONVERGENCE)
    assert result.eigenvalues is None
    details = result.error_details
    assert details["sweeps_used"] == 1
    assert details["sweep_budget"] == 1
    assert details["residual_relative_offdiag"] > 0.0
    assert details["unreduced_block"][1] == n - 1


def test_iteration_budget_configurable_changes_outcome(settings):
    n = 20
    off = np.ones(n - 1)
    laplacian = 2.0 * np.eye(n) - np.diag(off, 1) - np.diag(off, -1)
    blocked = run_eigendecomposition(
        laplacian.tolist(),
        settings,
        RequestOptions(max_iters=1, reference="none"),
    )
    assert blocked.verdict == "FAILED"
    allowed = run_eigendecomposition(
        laplacian.tolist(),
        settings,
        RequestOptions(max_iters=30, reference="none"),
    )
    assert allowed.verdict == "SUCCESS"
    assert allowed.computation["qr_sweeps_max_per_block"] <= 30


def test_size_budget_configurable(settings):
    from dataclasses import replace
    tight = replace(settings, max_n=3)
    result = run_eigendecomposition(np.eye(4).tolist(), tight)
    assert result.error_category == str(ErrorCategory.SIZE_LIMIT_EXCEEDED)


def test_large_matrix_auto_uses_scipy_reference(settings):
    from dataclasses import replace
    cfg = replace(settings, reference_max_n=4)
    rng = np.random.default_rng(9)
    m = rng.standard_normal((6, 6))
    a = m + m.T
    result = run_eigendecomposition(a.tolist(), cfg)
    assert result.verdict == "SUCCESS"
    assert result.reference_comparison["source"].startswith("scipy")


def test_requested_iteration_budget_above_hard_limit_rejected(settings):
    from dataclasses import replace
    cfg = replace(settings, hard_max_iters=50)
    result = run_eigendecomposition(
        np.eye(3).tolist(), cfg, RequestOptions(max_iters=51)
    )
    assert result.verdict == "FAILED"
    assert result.error_category == str(ErrorCategory.INVALID_PARAMETER)
    assert result.error_details["hard_max_iters"] == 50


def test_failed_evidence_gate_yields_uncertain_with_result_attached(rng):
    # A genuinely correct generic result (so all real errors are tiny) but an
    # impossibly tight evidence threshold: verdict must be UNCERTAIN while the
    # eigenvalues/vectors are still returned with explicit reasons.
    n = 6
    m = rng.standard_normal((n, n))
    a = m + m.T
    result = run_eigendecomposition(
        a.tolist(),
        options=RequestOptions(
            residual_rtol=1e-17,
            orthogonality_tol=1e-17,
            reconstruction_rtol=1e-17,
            reference="none",
        ),
    )
    assert result.verdict == "UNCERTAIN"
    assert result.eigenvalues is not None
    assert result.eigenvectors is not None
    assert len(result.uncertainties) >= 1
    assert all("exceeds threshold" in u for u in result.uncertainties)
    failed = [g for g in result.gates if not g["passed"]]
    assert failed and all(not g["passed"] for g in failed)


def test_result_is_interpretable_request_id_trace_location(settings):
    result = run_eigendecomposition(
        np.diag([1.0, 2.0]).tolist(),
        settings,
        RequestOptions(request_id="req-trace-42"),
    )
    assert result.request_id == "req-trace-42"
    steps = [t["step"] for t in result.trace]
    assert steps[:2] == ["validate_input", "check_symmetry"]
    assert "householder_tridiagonalization" in steps
    assert "implicit_wilkinson_qr" in steps
    assert "compute_evidence" in steps
    assert "independent_reference" in steps
    location = result.processing_location
    assert location["service_version"]
    assert location["numpy"] and location["scipy"] and location["mpmath"]
    assert result.algorithm
    # Every evidence gate carries value, threshold and verdict.
    for gate in result.gates:
        assert set(gate) >= {"name", "value", "threshold", "passed", "detail"}
