"""Tests for the numeric input boundary: exact parsing and failure codes."""
from __future__ import annotations

from fractions import Fraction as F

import pytest

from app.errors import FailureCode, InvalidPolynomialError
from app.numeric_io import parse_polynomial, parse_rational
from app.settings import KernelSettings


SETTINGS = KernelSettings(
    precision_dps=100, max_degree=4, max_coefficients=6,
    max_bisections=1000, max_sturm_length=6, max_coeff_bits=200,
)


def test_integer_and_fraction_strings_parse_exactly():
    assert parse_rational(7, index=0) == F(7)
    assert parse_rational("-3/7", index=0) == F(-3, 7)
    assert parse_rational("0.1", index=0) == F(1, 10)
    assert parse_rational("1.25e-3", index=0) == F(125, 100000)
    assert parse_rational("-2.5e2", index=0) == F(-250)


def test_binary_float_is_reconstructed_to_its_decimal_repr_exactly():
    # repr() makes 0.25 (exact in binary64) parse as 1/4.
    assert parse_rational(0.25, index=0) == F(1, 4)
    # Whatever binary value 1e-3 is, it parses through repr deterministically.
    assert parse_rational(1e-3, index=0) == F(repr(1e-3))


def test_boolean_rejected():
    with pytest.raises(InvalidPolynomialError) as exc:
        parse_rational(True, index=2)
    assert exc.value.code == FailureCode.INVALID_COEFFICIENTS


@pytest.mark.parametrize("bad", ["", "abc", "1/0", None, [], {}])
def test_malformed_tokens_rejected_with_specific_code(bad):
    with pytest.raises(InvalidPolynomialError) as exc:
        parse_rational(bad, index=0)
    assert exc.value.code == FailureCode.INVALID_COEFFICIENTS
    # A malformed string is redacted to its length, never echoed verbatim.
    if isinstance(bad, str) and bad:
        assert bad not in exc.value.message


def test_non_finite_float_rejected():
    with pytest.raises(InvalidPolynomialError) as exc:
        parse_rational(float("inf"), index=0)
    assert exc.value.code == FailureCode.INVALID_COEFFICIENTS
    assert exc.value.state["kind"] == "non-finite-float"


def test_empty_coefficient_list_is_empty_polynomial_error():
    with pytest.raises(InvalidPolynomialError) as exc:
        parse_polynomial([], SETTINGS)
    assert exc.value.code == FailureCode.EMPTY_POLYNOMIAL


def test_all_zero_list_parses_as_zero_polynomial_for_service_handling():
    poly = parse_polynomial([0, 0, 0], SETTINGS)
    assert len(poly) == 0  # trimmed; the service layer special-cases this


def test_trailing_and_leading_zeros_trim():
    poly = parse_polynomial([0, 1, 0], SETTINGS)
    assert poly == (F(0), F(1))  # x


def test_degree_budget_rejects_over_limit():
    with pytest.raises(InvalidPolynomialError) as exc:
        parse_polynomial([1, 1, 1, 1, 1, 1], SETTINGS)  # degree 5 > 4
    assert exc.value.code == FailureCode.DEGREE_EXCEEDED
    assert exc.value.state["degree"] == 5
    assert exc.value.state["limit"] == 4


def test_too_many_coefficients():
    with pytest.raises(InvalidPolynomialError) as exc:
        parse_polynomial([1] * 7, SETTINGS)
    assert exc.value.code == FailureCode.TOO_MANY_COEFFICIENTS


def test_coefficient_bit_budget():
    huge = F(10**100 + 1)
    with pytest.raises(InvalidPolynomialError) as exc:
        parse_polynomial([huge, 1], SETTINGS)
    assert exc.value.code == FailureCode.COEFFICIENT_TOO_LARGE
    assert exc.value.state["budget_bits"] == 200


def test_non_list_input_rejected():
    with pytest.raises(InvalidPolynomialError) as exc:
        parse_polynomial("1,2,3", SETTINGS)
    assert exc.value.code == FailureCode.INVALID_COEFFICIENTS
