"""Direct tests for the defensive input boundary and edge-case branches.

Pydantic rejects most malformed bodies before :mod:`app.services.inputio`
runs; these tests exercise the boundary functions directly so the
defense-in-depth validation and its failure categories are also covered.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from app.core import kernels
from app.services import synthetic
from app.services.inputio import InputValidationError, parse_values, validate_block_size


def test_parse_rejects_non_list():
    with pytest.raises(InputValidationError) as exc:
        parse_values({"a": 1})  # type: ignore[arg-type]
    assert exc.value.code == "values_not_array"


def test_parse_rejects_empty_list():
    with pytest.raises(InputValidationError) as exc:
        parse_values([])
    assert exc.value.code == "empty_input"


def test_parse_rejects_oversized_list(monkeypatch):
    from app.services import inputio
    from app.config import Settings

    small = Settings(
        max_input_length=2, default_block_size=4, reference_precision=80,
        reference_max_length=10, error_tolerance_factor=4.0, service_name="t",
    )
    monkeypatch.setattr(inputio, "settings", small)
    with pytest.raises(InputValidationError) as exc:
        parse_values([1.0, 2.0, 3.0])
    assert exc.value.code == "input_too_large"


def test_parse_accepts_ints_floats_and_special_tokens():
    arr = parse_values([1, 2.5, "NaN", "Infinity", "-Infinity"])
    assert arr.dtype == np.float64
    assert arr[0] == 1.0
    assert math.isnan(arr[2])
    assert arr[3] == math.inf
    assert arr[4] == -math.inf


def test_parse_rejects_boolean_with_index():
    with pytest.raises(InputValidationError) as exc:
        parse_values([1.0, True])
    assert exc.value.code == "non_numeric"
    assert exc.value.index == 1


def test_parse_rejects_bad_string_with_index():
    with pytest.raises(InputValidationError) as exc:
        parse_values([1.0, "xyz"])
    assert exc.value.code == "unparseable_token"
    assert exc.value.index == 1


def test_parse_rejects_object_type():
    with pytest.raises(InputValidationError) as exc:
        parse_values([[1.0]])
    assert exc.value.code == "non_numeric"
    assert exc.value.index == 0


def test_block_size_validation():
    assert validate_block_size(None) > 0
    assert validate_block_size(7) == 7
    for bad in (0, -3):
        with pytest.raises(InputValidationError) as exc:
            validate_block_size(bad)
        assert exc.value.code == "bad_block_size"


def test_error_payload_carries_category_message_index():
    err = InputValidationError("unparseable_token", "bad", index=3)
    payload = err.to_plain()
    assert payload == {"code": "unparseable_token", "message": "bad", "index": 3}


# ---------------------------------------------------------------------------
# Bound edge cases for tiny inputs.
# ---------------------------------------------------------------------------

def test_bounds_are_zero_for_single_element():
    from app.core.errors import kahan_bound, naive_bound, pairwise_bound

    assert naive_bound(1, 1e30) == 0.0
    assert kahan_bound(1, 1e30) == 0.0
    assert pairwise_bound(1, 32, 1e30) == 0.0


def test_pairwise_single_and_two_elements():
    assert kernels.pairwise_sum(np.array([3.25])) == 3.25
    assert kernels.pairwise_sum(np.array([1.0, 2.0])) == 3.0


def test_synthetic_rejects_unknown_scenario_and_bad_n():
    with pytest.raises(KeyError):
        synthetic.build_scenario("nope", 10)
    with pytest.raises(ValueError):
        synthetic.build_scenario("harmonic", 0)


def test_magnitude_buckets_for_specials():
    from app.services.diagnostics import magnitude_bucket

    assert magnitude_bucket(math.nan) == "NaN"
    assert magnitude_bucket(math.inf) == "+inf"
    assert magnitude_bucket(-math.inf) == "-inf"
    assert magnitude_bucket(0.0) == "+0"
    assert magnitude_bucket(-0.0) == "-0"
