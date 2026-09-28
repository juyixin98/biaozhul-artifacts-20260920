"""Input parsing and validation tests: concrete failure categories."""

from __future__ import annotations

import pytest

from app.input_parsing import MAX_DIMENSION, PayloadError, parse_system
from app.numerical import mpf

pytestmark = pytest.mark.unit


def test_parses_column_vector():
    parsed = parse_system({"A": [[2, 0], [0, 4]], "B": [8, 16]})
    assert parsed.n == 2
    assert parsed.nrhs == 1
    assert parsed.b[0, 0] == mpf(8)


def test_parses_matrix_of_rhs():
    parsed = parse_system(
        {"A": [[2, 0], [0, 4]], "B": [[8, 1], [16, 2]]}
    )
    assert parsed.nrhs == 2
    assert parsed.b[1, 1] == mpf(2)


def test_high_precision_string_entries_are_preserved():
    entry = "1.0000000000000000000000000000000000001"
    parsed = parse_system({"A": [["1", "0"], ["0", "1"]], "B": [entry, "0"]})
    assert parsed.string_entries >= 1
    assert parsed.b[0, 0] - mpf(1) > 0


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ({"B": [1, 2]}, "'A'"),
        ({"A": [[1, 0], [0, 1]]}, "'B'"),
        ({"A": [[1, 0, 0], [0, 1, 0]], "B": [1, 2]}, "square"),
        ({"A": [[1, 0], [0, 1]], "B": [1, 2, 3]}, "length"),
        ({"A": [[1, 0], [0, 1]], "B": [[1, 2], [3, 4, 5]]}, "same length"),
        ({"A": [["x", "0"], ["0", "1"]], "B": [1, 2]}, "A[0][0]"),
        ({"A": [[1, 0], [0, 1]], "B": ["notnum", 2]}, "B[0]"),
        ({"A": [], "B": []}, "non-empty"),
    ],
)
def test_malformed_payloads_raise_with_location(payload, fragment):
    with pytest.raises(PayloadError) as exc:
        parse_system(payload)
    assert fragment in str(exc.value)


def test_oversized_dimension_rejected():
    n = MAX_DIMENSION + 1
    payload = {
        "A": [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)],
        "B": [1.0] * n,
    }
    with pytest.raises(PayloadError, match="maximum"):
        parse_system(payload)
