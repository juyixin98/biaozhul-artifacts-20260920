"""Boundary validation tests: concrete failure categories, never generic."""

from __future__ import annotations

import numpy as np
import pytest

from toeplitz_fft.errors import ErrorCode, InputError
from toeplitz_fft.inputs import parse_problem


def _good_payload(**overrides: object) -> dict:
    payload = {
        "first_column": [1.0, 2.0, 3.0],
        "first_row": [1.0, 4.0, 5.0],
        "vectors": [1.0, 0.0, -1.0],
        "mode": "real",
        "precision": "double",
    }
    payload.update(overrides)
    return payload


def test_valid_payload_parses_shapes_and_batch() -> None:
    problem = parse_problem(_good_payload())
    assert problem.n == 3
    assert problem.batch == 1
    assert problem.value_dtype == np.dtype(np.float64)


def test_shared_element_mismatch_is_category() -> None:
    with pytest.raises(InputError) as exc:
        parse_problem(_good_payload(first_column=[9.0, 2.0, 3.0]))
    assert exc.value.code is ErrorCode.FIRST_ELEMENT_MISMATCH


def test_empty_vector_is_category() -> None:
    with pytest.raises(InputError) as exc:
        parse_problem(_good_payload(vectors=[]))
    assert exc.value.code is ErrorCode.EMPTY_INPUT


def test_nonfinite_is_category() -> None:
    with pytest.raises(InputError) as exc:
        parse_problem(_good_payload(first_column=[float("nan"), 2.0, 3.0]))
    assert exc.value.code is ErrorCode.NONFINITE_INPUT


def test_length_mismatch_is_category() -> None:
    with pytest.raises(InputError) as exc:
        parse_problem(_good_payload(vectors=[1.0, 0.0]))
    assert exc.value.code is ErrorCode.BATCH_LENGTH_MISMATCH


def test_inconsistent_batch_widths_is_category() -> None:
    with pytest.raises(InputError) as exc:
        parse_problem(_good_payload(vectors=[[1.0, 0.0, -1.0], [1.0, 0.0]]))
    assert exc.value.code is ErrorCode.BATCH_LENGTH_MISMATCH


def test_complex_spec_wrong_shape_is_category() -> None:
    with pytest.raises(InputError) as exc:
        parse_problem({
            "mode": "complex",
            "first_column": [[1.0, 0.0], [2.0, 1.0], [3.0, 0.0]],
            "first_row": [[1.0, 0.0], [4.0, -1.0], [5.0, 2.0]],
            # third entry is not a [re, im] pair
            "vectors": [[0.0, 1.0], [1.0, 0.0], [0.0]],
        })
    assert exc.value.code is ErrorCode.COMPLEX_SPEC_INVALID


def test_unknown_precision_is_category() -> None:
    with pytest.raises(InputError) as exc:
        parse_problem(_good_payload(precision="quad"))
    assert exc.value.code is ErrorCode.PRECISION_UNSUPPORTED


def test_complex_object_form_roundtrips() -> None:
    payload = {
        "mode": "complex",
        "first_column": {"real": [1.0, 2.0, 3.0], "imag": [0.0, 1.0, 0.0]},
        "first_row": {"real": [1.0, 4.0, 5.0], "imag": [0.0, -1.0, 2.0]},
        "vectors": [[0.0, 1.0], [1.0, 0.0], [0.0, 0.0]],
    }
    problem = parse_problem(payload)
    assert problem.is_complex
    assert problem.value_dtype == np.dtype(np.complex128)
    assert problem.first_column[1] == pytest.approx(2.0 + 1.0j)


def test_single_precision_selects_32bit_storage() -> None:
    problem = parse_problem(_good_payload(precision="single"))
    assert problem.value_dtype == np.dtype(np.float32)
