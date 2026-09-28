"""Unit tests for exact input parsing."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from rational_linalg.errors import InputError
from rational_linalg.numbers import parse_exact, parse_matrix, parse_vector


def test_fraction_and_decimal_instances_accepted_exactly():
    assert parse_exact(Fraction(7, 3)) == Fraction(7, 3)
    assert parse_exact(Decimal("0.25")) == Fraction(1, 4)
    assert parse_exact(Decimal(5)) == Fraction(5)


def test_decimal_non_finite_rejected():
    with pytest.raises(InputError) as exc:
        parse_exact(Decimal("NaN"))
    assert exc.value.code == "NON_FINITE_NUMBER"


def test_bad_string_variants_are_input_errors():
    for bad in ("1/0", "abc", "1.2.3"):
        with pytest.raises(InputError) as exc:
            parse_exact(bad)
        assert exc.value.code == "INVALID_NUMBER"


def test_non_list_matrix_shapes_rejected():
    with pytest.raises(InputError) as exc:
        parse_matrix((1, 2), name="A")  # type: ignore[arg-type]
    assert exc.value.code == "INVALID_MATRIX_SHAPE"
    with pytest.raises(InputError) as exc:
        parse_matrix([1, 2, 3], name="A")  # type: ignore[list-item]
    assert exc.value.code == "INVALID_MATRIX_SHAPE"


def test_vector_entries_have_indexed_error_paths():
    with pytest.raises(InputError) as exc:
        parse_vector([1, "x"], path="b")
    assert exc.value.details["path"] == "b[1]"


def test_integer_string_and_decimal_string_are_exact():
    assert parse_exact(3) == Fraction(3)
    assert parse_exact("3") == Fraction(3)
    assert parse_exact("3/2") == Fraction(3, 2)
    # The classical float trap: 0.1 as an exact string is 1/10, never the
    # binary-float approximation.
    assert parse_exact("0.1") == Fraction(1, 10)
    assert parse_exact("1.5e3") == Fraction(1500)
    assert parse_exact("-7/4") == Fraction(-7, 4)


def test_float_input_is_rejected_not_silently_converted():
    with pytest.raises(InputError) as exc:
        parse_exact(0.1)
    assert exc.value.code == "NON_EXACT_NUMBER"
    with pytest.raises(InputError):
        parse_exact(1.0)  # even "integer-looking" floats are rejected


def test_bool_nan_infinity_rejected():
    with pytest.raises(InputError) as exc:
        parse_exact(True)
    assert exc.value.code == "INVALID_NUMBER"
    for bad in ("nan", "inf", "-inf", "infinity", "  "):
        with pytest.raises(InputError):
            parse_exact(bad)


def test_matrix_shape_validation():
    with pytest.raises(InputError) as exc:
        parse_matrix([[1, 2], [3]], name="A")
    assert exc.value.code == "RAGGED_MATRIX"
    with pytest.raises(InputError) as exc:
        parse_matrix([], name="A")
    assert exc.value.code == "EMPTY_MATRIX"
    with pytest.raises(InputError) as exc:
        parse_matrix([[]], name="A")
    assert exc.value.code == "EMPTY_MATRIX"


def test_parsed_matrix_uses_fractions_and_is_a_copy():
    original = [["1/2", 2], ["0.25", "-4"]]
    parsed = parse_matrix(original, name="A")
    assert parsed == [[Fraction(1, 2), Fraction(2)],
                      [Fraction(1, 4), Fraction(-4)]]
    parsed[0][0] = Fraction(99)
    assert original[0][0] == "1/2"


def test_vector_empty_rejected():
    with pytest.raises(InputError) as exc:
        parse_vector([], path="b")
    assert exc.value.code == "EMPTY_MATRIX"
