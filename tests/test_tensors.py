"""Layer 1: tensor value-object validation and batch-rejection categories."""

from __future__ import annotations

import numpy as np
import pytest

from sparse_embedding.config import TableSpec
from sparse_embedding.errors import EmptyBatchError, ValidationBatchRejectedError
from sparse_embedding.tensors import FLOAT_DTYPE, SparseGradientBatch

SPEC = TableSpec(num_rows=6, dim=2)


def batch(indices, values):
    return SparseGradientBatch.from_pairs(indices, values, SPEC)


def test_accepts_well_formed_batch_and_normalises_dtype():
    b = batch([0, 1], [[1, 2], [3, 4]])
    assert b.size == 2
    assert b.values.dtype == FLOAT_DTYPE
    assert b.indices.dtype == np.int64


def test_duplicate_indices_are_structurally_allowed():
    b = batch([0, 0, 0], [[1, 1], [1, 1], [1, 1]])
    assert b.size == 3  # aggregation happens later; duplicates retained here


def test_rejects_out_of_range_high_index_entire_batch():
    with pytest.raises(ValidationBatchRejectedError) as exc:
        batch([0, 6], [[1, 1], [1, 1]])
    assert exc.value.code == "batch_rejected"
    assert exc.value.details["bad_index"] == 6
    assert exc.value.details["n_bad"] == 1


def test_rejects_negative_index_entire_batch():
    with pytest.raises(ValidationBatchRejectedError) as exc:
        batch([-1, 2], [[1, 1], [1, 1]])
    assert exc.value.code == "batch_rejected"
    assert exc.value.details["bad_index"] == -1


def test_single_bad_index_rejects_all_even_if_others_valid():
    # The good row must not survive: whole-batch rejection is all-or-nothing.
    with pytest.raises(ValidationBatchRejectedError):
        batch([1, 2, 999], [[1, 1], [1, 1], [1, 1]])


def test_rejects_length_mismatch():
    with pytest.raises(ValidationBatchRejectedError) as exc:
        batch([0, 1, 2], [[1, 1], [1, 1]])
    assert exc.value.code == "batch_rejected"
    assert exc.value.details["n_indices"] == 3
    assert exc.value.details["n_values"] == 2


def test_rejects_width_mismatch():
    with pytest.raises(ValidationBatchRejectedError) as exc:
        batch([0], [[1, 2, 3]])
    assert exc.value.details["expected_dim"] == 2


def test_rejects_non_integer_indices():
    with pytest.raises(ValidationBatchRejectedError):
        batch(np.array([0.5, 1.0]), [[1, 1], [1, 1]])


def test_rejects_non_finite_gradients():
    with pytest.raises(ValidationBatchRejectedError) as exc:
        batch([0], [[np.nan, 1.0]])
    assert "finite" in exc.value.message
    with pytest.raises(ValidationBatchRejectedError):
        batch([0], [[np.inf, 1.0]])


def test_empty_batch_is_constructible_but_flagged_explicitly():
    b = batch([], [])
    assert b.size == 0
    with pytest.raises(EmptyBatchError) as exc:
        b.require_non_empty()
    assert exc.value.code == "empty_batch"
