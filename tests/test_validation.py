"""Tests for request validation and bounds."""

import pytest

from robustreg.validation import MAX_N, MAX_P, RequestError, parse_request


def _req(**overrides):
    base = {"X": [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]],
            "y": [1.0, 2.0, 3.0]}
    base.update(overrides)
    return base


def test_valid_defaults():
    p = parse_request(_req())
    assert p.method == "huber"
    assert p.fit_intercept is True
    assert p.delta == 1.345
    assert p.lam == 0.0
    assert p.max_iter == 50
    assert p.tol == 1e-8
    assert p.rcond is None
    assert p.allow_rank_deficient is False


@pytest.mark.parametrize("field,value", [
    ("X", "notalist"),
    ("X", [[1.0], [2.0, 3.0]]),      # ragged rows
    (
        "X",
        [[1.0, 0.0], [0.0]],          # ragged rows
    ),
    ("X", [[1.0, "a"], [0.0, 1.0]]),
    ("X", [[1.0, float("nan")], [0.0, 1.0]]),
    ("X", [[1.0, float("inf")], [0.0, 1.0]]),
    ("X", []),
    ("X", [[]]),
    ("y", 123),
    ("y", [1.0, float("nan"), 0.0]),
    ("y", [[1.0], [2.0], [3.0]]),
])
def test_malformed_matrices(field, value):
    req = _req(**{field: value})
    with pytest.raises(RequestError):
        parse_request(req)


def test_shape_mismatch():
    with pytest.raises(RequestError, match="length of y"):
        parse_request({"X": [[1.0], [2.0]], "y": [1.0]})


def test_missing_fields():
    with pytest.raises(RequestError, match="'X'"):
        parse_request({"y": [1.0]})
    with pytest.raises(RequestError, match="'y'"):
        parse_request({"X": [[1.0]]})


def test_unknown_field_rejected():
    with pytest.raises(RequestError, match="unknown field"):
        parse_request(_req(epislon=0.1))


def test_request_not_object():
    with pytest.raises(RequestError):
        parse_request([1, 2, 3])


def test_bad_method():
    with pytest.raises(RequestError, match="method"):
        parse_request(_req(method="lasso"))


@pytest.mark.parametrize("field,value", [
    ("fit_intercept", 1),
    ("allow_rank_deficient", "yes"),
    ("delta", -1.0),
    ("delta", 0.0),
    ("delta", float("inf")),
    ("lam", -0.01),
    ("max_iter", 0),
    ("max_iter", -3),
    ("tol", 0.0),
    ("tol", 2.0),
    ("rcond", 0.0),
    ("rcond", 1e-20),
])
def test_bad_scalar_fields(field, value):
    with pytest.raises(RequestError):
        parse_request(_req(**{field: value}))


def test_explicit_null_rcond_ok():
    p = parse_request(_req(rcond=None))
    assert p.rcond is None


def test_size_limits_enforced():
    big_n = {"X": [[0.0]] * (MAX_N + 1), "y": [0.0] * (MAX_N + 1)}
    with pytest.raises(RequestError, match="exceeds maximum"):
        parse_request(big_n)
    row = [0.0] * (MAX_P + 1)
    big_p = {"X": [row, row], "y": [0.0, 0.0]}
    with pytest.raises(RequestError, match="exceeds maximum"):
        parse_request(big_p)


def test_data_magnitude_limit():
    with pytest.raises(RequestError, match="1e\\+100"):
        parse_request({"X": [[1e150]], "y": [1.0]})


def test_bool_is_not_a_number():
    with pytest.raises(RequestError):
        parse_request(_req(delta=True))
