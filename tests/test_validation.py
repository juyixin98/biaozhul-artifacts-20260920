"""Input boundary tests: normalization and rejection categories."""

import numpy as np
import pytest

from app.errors import ErrorCategory, InputInvalidError, ResourceExhaustedError
from app.validation import normalize, parse_coefficients


class TestParse:
    def test_valid_pairs(self):
        coeffs = parse_coefficients([[1.0, 0.0], [-6.0, 0.0], [11.0, 0.0], [-6.0, 0.0]])
        assert coeffs.dtype == np.complex128
        assert list(coeffs) == [1, -6, 11, -6]

    def test_empty_list_rejected(self):
        with pytest.raises(InputInvalidError) as ei:
            parse_coefficients([])
        assert ei.value.category is ErrorCategory.INPUT_INVALID

    def test_non_finite_rejected(self):
        with pytest.raises(InputInvalidError):
            parse_coefficients([[1.0, 0.0], [float("nan"), 0.0]])
        with pytest.raises(InputInvalidError):
            parse_coefficients([[1.0, 0.0], [float("inf"), 0.0]])

    def test_malformed_pair_rejected(self):
        with pytest.raises(InputInvalidError):
            parse_coefficients([[1.0, 0.0], ["abc", 0.0]])


class TestNormalize:
    def test_leading_zeros_stripped(self):
        # 0*z^4 + 0*z^3 + z^2 - 3z + 2 must normalize to degree 2.
        poly = normalize(parse_coefficients(
            [[0.0, 0.0], [0.0, 0.0], [1.0, 0.0], [-3.0, 0.0], [2.0, 0.0]]
        ), max_degree=100)
        assert poly.degree == 2
        assert poly.leading_dropped == 2
        np.testing.assert_array_equal(poly.coeffs, [1, -3, 2])

    def test_zero_polynomial_rejected(self):
        with pytest.raises(InputInvalidError) as ei:
            normalize(parse_coefficients([[0.0, 0.0], [0.0, 0.0]]), max_degree=100)
        assert ei.value.category is ErrorCategory.INPUT_INVALID
        assert "zero polynomial" in ei.value.message

    def test_tiny_scale_polynomial_accepted(self):
        # A polynomial whose coefficients are all tiny but nonzero is NOT
        # the zero polynomial: the leading-zero tolerance is relative to
        # the input scale, so it must normalize and solve normally.
        poly = normalize(parse_coefficients([[1e-300, 0.0], [1e-300, 0.0]]), max_degree=100)
        assert poly.degree == 1

    def test_constant_polynomial_rejected(self):
        with pytest.raises(InputInvalidError) as ei:
            normalize(parse_coefficients([[5.0, 0.0]]), max_degree=100)
        assert "degree 0" in ei.value.message

    def test_degree_cap_is_resource_error(self):
        coeffs = [[1.0, 0.0]] + [[0.0, 0.0]] * 50 + [[1.0, 0.0]]  # degree 51
        with pytest.raises(ResourceExhaustedError) as ei:
            normalize(parse_coefficients(coeffs), max_degree=50)
        assert ei.value.category is ErrorCategory.RESOURCE_EXHAUSTED
