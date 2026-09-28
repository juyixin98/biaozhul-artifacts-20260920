"""Evidence layer tests.

These prove the verification machinery has teeth: it passes correct results
and assigns the explicit ``numeric_accuracy`` failure category to corrupted
results.  Oracle answers are checked against hand-computed values, so the
reference cannot secretly be the kernel under test.
"""

from __future__ import annotations

import numpy as np
import pytest

from toeplitz_fft.errors import ErrorCode
from toeplitz_fft.evidence import (
    deterministic_sample_indices,
    error_metrics,
    mpmath_oracle,
    scipy_dense_oracle,
    verify_result,
)
from toeplitz_fft.fixtures import fixture_to_payload, make_complex_non_hermitian
from toeplitz_fft.inputs import parse_problem


C3 = np.array([1.0, 2.0, 3.0])
R3 = np.array([1.0, 4.0, 5.0])
X3 = np.array([[1.0, 0.0, -1.0]])
# Hand computed: T @ [1, 0, -1] = [-4, -2, 2]
HAND_Y = np.array([[-4.0, -2.0, 2.0]])


def test_mpmath_oracle_matches_hand_computed_answer() -> None:
    ref = mpmath_oracle(C3, R3, X3)
    np.testing.assert_allclose(ref, HAND_Y, rtol=0, atol=1e-40)


def test_two_independent_oracles_agree() -> None:
    ref_mp = mpmath_oracle(C3, R3, X3)
    ref_dense = scipy_dense_oracle(C3, R3, X3)
    np.testing.assert_allclose(ref_mp, ref_dense, rtol=1e-13, atol=1e-13)


def test_complex_oracle_agrees_with_dense() -> None:
    fx = make_complex_non_hermitian(11)
    problem = parse_problem(fixture_to_payload(fx, mode="complex"))
    ref_mp = mpmath_oracle(problem.first_column, problem.first_row,
                           problem.vectors)
    ref_dense = scipy_dense_oracle(
        problem.first_column.astype(np.complex128),
        problem.first_row.astype(np.complex128),
        problem.vectors.astype(np.complex128))
    np.testing.assert_allclose(ref_mp, ref_dense, rtol=1e-12, atol=1e-12)


def _problem():
    return parse_problem({
        "first_column": list(C3),
        "first_row": list(R3),
        "vectors": [list(X3[0])],
        "mode": "real",
        "precision": "double",
    })


def test_correct_result_passes_evidence() -> None:
    problem = _problem()
    report = verify_result(problem, HAND_Y, cache_hit=True,
                           kernel_path="embedding_fft", memory={},
                           case_id="unit-correct")
    assert report.passed is True
    assert report.failure_category is None
    assert report.against_mpmath["max_abs_error"] < 1e-12
    assert report.oracle_agreement["used"] is True


def test_corrupted_result_gets_numeric_accuracy_category() -> None:
    problem = _problem()
    corrupted = HAND_Y.copy()
    corrupted[0, 0] += 1.0  # deliberately wrong
    report = verify_result(problem, corrupted, cache_hit=False,
                           kernel_path="embedding_fft", memory={},
                           case_id="unit-corrupt")
    assert report.passed is False
    assert report.failure_category == ErrorCode.NUMERIC_ACCURACY.value
    assert report.against_mpmath["max_abs_error"] == pytest.approx(1.0,
                                                                   abs=1e-9)
    assert report.against_scipy_dense["passed"] is False


def test_metrics_scale_aware_gate() -> None:
    good = error_metrics(np.array([1.0, 2.0]), np.array([1.0, 2.0]), "double")
    assert good.passed
    bad = error_metrics(np.array([1.5, 2.5]), np.array([1.0, 2.0]), "double")
    assert not bad.passed
    assert bad.max_abs_error == pytest.approx(0.5)


def test_single_precision_looser_gate_still_catches_corruption() -> None:
    problem = parse_problem({
        "first_column": list(C3), "first_row": list(R3),
        "vectors": [list(X3[0])], "mode": "real", "precision": "single",
    })
    corrupted = HAND_Y.astype(np.float32)
    corrupted[0, 1] += 0.1
    report = verify_result(problem, corrupted, cache_hit=False,
                           kernel_path="embedding_fft", memory={},
                           case_id="unit-sp-corrupt")
    assert report.passed is False
    assert report.failure_category == ErrorCode.NUMERIC_ACCURACY.value


def test_sampled_oracle_matches_hand_answer_at_probes() -> None:
    from toeplitz_fft.evidence import (
        deterministic_sample_indices,
        mpmath_sampled_oracle,
    )
    rng = np.random.default_rng(3)
    n = 64  # large-n style, but exact probes are still instant
    c, r = rng.standard_normal(n), rng.standard_normal(n)
    r[0] = c[0]
    x = rng.standard_normal((2, n))
    probes = deterministic_sample_indices(n, 2, 12)
    assert (0, 0) in probes and (0, n - 1) in probes  # edges always covered
    sampled = mpmath_sampled_oracle(c, r, x, probes)
    full_dense = scipy_dense_oracle(c, r, x)
    for b, i in probes:
        assert sampled[b, i] == pytest.approx(full_dense[b, i], abs=1e-12)
    assert np.ma.is_masked(sampled[0, 1]) is True or (0, 1) in probes


def test_verify_sampled_mode_flags_sampling_and_passes() -> None:
    rng = np.random.default_rng(4)
    n = 48
    c, r = rng.standard_normal(n), rng.standard_normal(n)
    r[0] = c[0]
    x = rng.standard_normal((1, n))
    problem = parse_problem({
        "first_column": list(c), "first_row": list(r),
        "vectors": [list(x[0])], "mode": "real", "precision": "double",
    })
    actual = scipy_dense_oracle(c, r, x)
    probes = deterministic_sample_indices(n, 1, 10)
    report = verify_result(problem, actual, cache_hit=False,
                           kernel_path="embedding_fft", memory={},
                           case_id="unit-sampled", sample_indices=probes)
    assert report.passed is True
    assert report.against_mpmath["sampled"] is True
    assert report.against_mpmath["probe_count"] == len(probes)
    assert report.against_scipy_dense["used"] is False
