"""Input validation tests: shape, finiteness, size and symmetry checks."""

import numpy as np
import pytest

from sym_eig.config import Settings
from sym_eig.errors import EigServiceError, ErrorCategory
from sym_eig.numerical.validation import ensure_symmetric, to_finite_matrix


def _settings(**overrides):
    return Settings(max_n=4, **overrides)


def test_rejects_empty_matrix():
    with pytest.raises(EigServiceError) as exc:
        to_finite_matrix([], _settings())
    assert exc.value.category is ErrorCategory.INVALID_MATRIX


def test_rejects_ragged_matrix():
    with pytest.raises(EigServiceError) as exc:
        to_finite_matrix([[1.0, 0.0], [2.0]], _settings())
    assert exc.value.category is ErrorCategory.INVALID_MATRIX
    assert exc.value.details["row_index"] == 1


def test_rejects_non_square():
    with pytest.raises(EigServiceError) as exc:
        to_finite_matrix([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], _settings())
    assert exc.value.category is ErrorCategory.INVALID_MATRIX
    assert exc.value.details["rows"] == 2
    assert exc.value.details["columns"] == 3


def test_rejects_non_numeric_and_boolean():
    for bad in ("x", None, [1.0], True):
        with pytest.raises(EigServiceError) as exc:
            to_finite_matrix([[1.0, bad], [0.0, 1.0]], _settings())
        assert exc.value.category is ErrorCategory.INVALID_MATRIX


def test_rejects_nan_and_infinity():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(EigServiceError) as exc:
            to_finite_matrix([[1.0, bad], [bad, 1.0]], _settings())
        assert exc.value.category is ErrorCategory.INVALID_MATRIX


def test_rejects_size_above_budget():
    matrix = np.eye(5).tolist()
    with pytest.raises(EigServiceError) as exc:
        to_finite_matrix(matrix, _settings())
    assert exc.value.category is ErrorCategory.SIZE_LIMIT_EXCEEDED
    assert exc.value.details["n"] == 5
    assert exc.value.details["max_n"] == 4


def test_symmetry_uses_relative_tolerance():
    # A skew of 1e-6 is rejected at rtol=1e-10 on an O(1) matrix ...
    a = np.array([[1.0, 1.0e-6], [0.0, 1.0]])
    ok, deviation, threshold, sym = ensure_symmetric(
        a, _settings(symmetry_rtol=1e-10)
    )
    assert not ok
    assert deviation == pytest.approx(1e-6)
    assert threshold < 1e-6

    # ... but the SAME absolute skew is acceptable on an O(1e8) matrix where
    # it is a 1e-14 relative perturbation.
    big = np.array([[1.0e8, 5.0e-7], [-5.0e-7, 1.0e8]])
    ok2, deviation2, threshold2, sym2 = ensure_symmetric(
        big, _settings(symmetry_rtol=1e-10)
    )
    assert ok2
    assert deviation2 / max(1.0, np.max(np.abs(big))) < 1e-10
    np.testing.assert_allclose(sym2, (big + big.T) / 2)


def test_exact_symmetric_passes():
    a = np.array([[1.0, 2.0, 3.0], [2.0, 4.0, 5.0], [3.0, 5.0, 6.0]])
    ok, deviation, _, _ = ensure_symmetric(a, _settings())
    assert ok and deviation == 0.0
