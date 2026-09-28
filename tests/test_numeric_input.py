"""Boundary validation tests: every input failure class is distinguishable."""
from __future__ import annotations

import pytest

from rationalsvc import numeric_input
from rationalsvc.errors import (
    ErrorCode,
    InputEmpty,
    InputMalformed,
    InputNotRepresentable,
    InputPrecisionUnsupported,
    InputShapeMismatch,
    StateConflict,
)


def test_integer_decimal_and_ratio_strings_accepted():
    parsed = numeric_input.parse_request(
        {"A": [[1, "2/3", "-0.25", "1e3"]], "b": ["1/7"]}
    )
    from fractions import Fraction
    assert parsed.A[0] == [
        Fraction(1), Fraction(2, 3), Fraction(-1, 4), Fraction(1000)
    ]
    assert parsed.b[0][0] == Fraction(1, 7)


def test_float_is_refused_with_distinct_error():
    with pytest.raises(InputPrecisionUnsupported) as ei:
        numeric_input.parse_request({"A": [[1, 0.5]], "b": [1]})
    assert ei.value.category is ErrorCode.INPUT_PRECISION_UNSUPPORTED
    assert ei.value.http_status == 400
    assert "0.5" in ei.value.details["offender"]


def test_bool_is_refused():
    with pytest.raises(InputMalformed):
        numeric_input.parse_request({"A": [[True]], "b": [1]})


def test_nan_and_inf_strings_refused():
    for bad in ("nan", "inf", "1/0"):
        with pytest.raises((InputNotRepresentable, InputMalformed, ZeroDivisionError)):
            numeric_input.parse_request({"A": [[bad]], "b": [1]})


def test_ragged_rows_are_shape_mismatch():
    with pytest.raises(InputShapeMismatch) as ei:
        numeric_input.parse_request({"A": [[1, 2, 3], [4, 5]], "b": [1, 2]})
    assert ei.value.category is ErrorCode.INPUT_SHAPE_MISMATCH
    assert ei.value.details["row_index"] == 1


def test_empty_matrix_is_input_empty():
    with pytest.raises(InputEmpty) as ei:
        numeric_input.parse_request({"A": [], "b": []})
    assert ei.value.category is ErrorCode.INPUT_EMPTY


def test_b_vector_length_mismatch():
    with pytest.raises(InputShapeMismatch) as ei:
        numeric_input.parse_request(
            {"A": [[1, 0], [0, 1]], "b": [1, 2, 3]}
        )
    assert ei.value.details == {"expected": 2, "got": 3}


def test_missing_fields_are_malformed():
    with pytest.raises(InputMalformed) as ei:
        numeric_input.parse_request({"b": [1]})
    assert ei.value.category is ErrorCode.INPUT_MALFORMED


def test_non_object_body_is_malformed():
    with pytest.raises(InputMalformed):
        numeric_input.parse_request([1, 2, 3])


def test_b_matrix_form_accepted_with_two_rhs():
    parsed = numeric_input.parse_request(
        {"A": [[1, 0], [0, 1]], "b": [[1, 2], [3, 4]]}
    )
    assert parsed.rhs_count == 2
    assert parsed.b[0] == [1, 2]
    assert parsed.b[1] == [3, 4]


def test_zero_budget_is_state_conflict_not_input_error():
    with pytest.raises(StateConflict) as ei:
        numeric_input.parse_request({"A": [[1]], "b": [1], "digit_budget": 0})
    assert ei.value.category is ErrorCode.STATE_CONFLICT
    assert ei.value.http_status == 409


def test_budget_above_ceiling_rejected():
    with pytest.raises(StateConflict):
        numeric_input.parse_request(
            {"A": [[1]], "b": [1], "digit_budget": 10 ** 9}
        )


def test_unknown_want_is_state_conflict():
    with pytest.raises(StateConflict):
        numeric_input.parse_request({"A": [[1]], "b": [1], "want": "nonsense"})


def test_default_budget_applied():
    parsed = numeric_input.parse_request({"A": [[1]], "b": [1]})
    assert parsed.digit_budget == numeric_input.DEFAULT_DIGIT_BUDGET
