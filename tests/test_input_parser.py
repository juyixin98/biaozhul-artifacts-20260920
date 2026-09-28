"""Input parsing and budget tests: exactness, categories, redaction."""

from __future__ import annotations

from fractions import Fraction as F

import pytest

from root_isolator.errors import ErrorCategory, IsolationError
from root_isolator.input.parser import (
    parse_dense_coefficients,
    parse_fraction,
    parse_sparse_coefficients,
)


# --------------------------------------------------------------------------- #
# Exact fraction parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value,expected",
    [
        (3, F(3)),
        (-7, F(-7)),
        ("1/10", F(1, 10)),
        ("-3/7", F(-3, 7)),
        ("0.25", F(1, 4)),
        ("1e-3", F(1, 1000)),
        ("  2/5 ", F(2, 5)),
    ],
)
def test_parse_fraction_exact(value, expected):
    assert parse_fraction(value, index=0) == expected


@pytest.mark.parametrize("value", [0.1, 1.5, float(2)])
def test_binary_float_is_rejected_with_specific_category(value):
    with pytest.raises(IsolationError) as exc:
        parse_fraction(value, index=2)
    assert exc.value.category is ErrorCategory.NON_EXACT_COEFFICIENT
    assert exc.value.state["index"] == 2


def test_bool_is_rejected():
    with pytest.raises(IsolationError) as exc:
        parse_fraction(True, index=0)
    assert exc.value.category is ErrorCategory.MALFORMED_COEFFICIENTS


@pytest.mark.parametrize("bad", ["abc", "1/0", "1//2", ""])
def test_malformed_strings_are_rejected(bad):
    with pytest.raises(IsolationError) as exc:
        parse_fraction(bad, index=0)
    assert exc.value.category is ErrorCategory.MALFORMED_COEFFICIENTS


def test_unsupported_type_rejected():
    with pytest.raises(IsolationError) as exc:
        parse_fraction([1], index=0)
    assert exc.value.category is ErrorCategory.MALFORMED_COEFFICIENTS


# --------------------------------------------------------------------------- #
# Dense ordering
# --------------------------------------------------------------------------- #
def test_descending_order_maps_high_power_first(budget):
    parsed = parse_dense_coefficients([1, 0, -3, 2], order="descending", budget=budget)
    # x^3 - 3x + 2 -> ascending [2,-3,0,1]
    assert list(parsed.polynomial.coeffs) == [F(2), F(-3), F(0), F(1)]
    assert parsed.effective_degree == 3


def test_ascending_order(budget):
    parsed = parse_dense_coefficients([2, -3, 0, 1], order="ascending", budget=budget)
    assert list(parsed.polynomial.coeffs) == [F(2), F(-3), F(0), F(1)]


def test_trailing_zero_degree_reported(budget):
    parsed = parse_dense_coefficients([1, 2, 0, 0], order="ascending", budget=budget)
    assert parsed.declared_degree == 3
    assert parsed.effective_degree == 1


def test_empty_list_specific_category(budget):
    with pytest.raises(IsolationError) as exc:
        parse_dense_coefficients([], order="descending", budget=budget)
    assert exc.value.category is ErrorCategory.EMPTY_COEFFICIENTS


def test_non_list_payload(budget):
    with pytest.raises(IsolationError) as exc:
        parse_dense_coefficients("1,2,3", order="descending", budget=budget)  # type: ignore[arg-type]
    assert exc.value.category is ErrorCategory.MALFORMED_COEFFICIENTS


def test_bad_order(budget):
    with pytest.raises(IsolationError) as exc:
        parse_dense_coefficients([1, 2], order="sideways", budget=budget)
    assert exc.value.category is ErrorCategory.MALFORMED_COEFFICIENTS


# --------------------------------------------------------------------------- #
# Sparse format
# --------------------------------------------------------------------------- #
def test_sparse_parsing(budget):
    parsed = parse_sparse_coefficients({"0": -1, "2": 1}, budget=budget)
    assert list(parsed.polynomial.coeffs) == [F(-1), F(0), F(1)]
    assert parsed.effective_degree == 2


def test_sparse_negative_power_rejected(budget):
    with pytest.raises(IsolationError) as exc:
        parse_sparse_coefficients({"-1": 1}, budget=budget)
    assert exc.value.category is ErrorCategory.MALFORMED_COEFFICIENTS


def test_sparse_non_integer_power_rejected(budget):
    with pytest.raises(IsolationError) as exc:
        parse_sparse_coefficients({"1.5": 1}, budget=budget)
    assert exc.value.category is ErrorCategory.MALFORMED_COEFFICIENTS


def test_sparse_empty_rejected(budget):
    with pytest.raises(IsolationError) as exc:
        parse_sparse_coefficients({}, budget=budget)
    assert exc.value.category is ErrorCategory.EMPTY_COEFFICIENTS


def test_sparse_not_mapping(budget):
    with pytest.raises(IsolationError) as exc:
        parse_sparse_coefficients([1, 2], budget=budget)  # type: ignore[arg-type]
    assert exc.value.category is ErrorCategory.MALFORMED_COEFFICIENTS


# --------------------------------------------------------------------------- #
# Budgets
# --------------------------------------------------------------------------- #
def test_degree_budget_enforced(tight_budget):
    coeffs = [1] * 10  # degree 9 > max_degree 8
    with pytest.raises(IsolationError) as exc:
        parse_dense_coefficients(coeffs, order="ascending", budget=tight_budget)
    assert exc.value.category is ErrorCategory.DEGREE_EXCEEDED
    assert exc.value.state["degree"] == 9


def test_coefficient_size_budget_enforced(tight_budget):
    huge = 10 ** 200  # ~665 bits > 256
    with pytest.raises(IsolationError) as exc:
        parse_dense_coefficients([1, huge], order="ascending", budget=tight_budget)
    assert exc.value.category is ErrorCategory.COEFFICIENT_TOO_LARGE
    assert exc.value.state["bits"] > 256


def test_sparse_degree_budget(tight_budget):
    with pytest.raises(IsolationError) as exc:
        parse_sparse_coefficients({str(100): 1}, budget=tight_budget)
    assert exc.value.category is ErrorCategory.DEGREE_EXCEEDED


# --------------------------------------------------------------------------- #
# Redaction fingerprint
# --------------------------------------------------------------------------- #
def test_fingerprint_is_stable_and_non_reversible(budget):
    a = parse_dense_coefficients([1, 0, -3, 2], order="descending", budget=budget)
    b = parse_dense_coefficients([1, 0, -3, 2], order="descending", budget=budget)
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint.startswith("deg=3;terms=3;hash64=")
    # Fixed 16-hex-char digest, bounded length -> no payload embedded.
    digest = a.fingerprint.split("hash64=")[1]
    assert len(digest) == 16
    assert all(ch in "0123456789abcdef" for ch in digest)


def test_fingerprint_hides_distinctive_coefficient(budget):
    secret = 987654321123456789
    parsed = parse_dense_coefficients([1, secret], order="ascending", budget=budget)
    assert str(secret) not in parsed.fingerprint


def test_fingerprint_distinguishes_different_polynomials(budget):
    a = parse_dense_coefficients([1, 0, 1], order="ascending", budget=budget)   # x^2+1
    b = parse_dense_coefficients([1, 0, 2], order="ascending", budget=budget)   # x^2+2
    assert a.fingerprint != b.fingerprint


def test_zero_polynomial_flag(budget):
    parsed = parse_dense_coefficients([0, 0], order="ascending", budget=budget)
    assert parsed.is_zero is True
    assert parsed.effective_degree == -1
