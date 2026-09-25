"""向量合并与校验语义测试。"""

from __future__ import annotations

import numpy as np
import pytest

from sparse_retrieval.vector import (
    SparseVector,
    SparseVectorError,
    l2_norm,
    merge_entries,
    normalize_entries,
    parse_entries,
)

pytestmark = pytest.mark.unit


def test_merge_sums_duplicate_indices() -> None:
    idx, val = merge_entries([2, 0, 2, 0], [1.0, 3.0, 4.0, -1.0], dim=5)
    assert idx.tolist() == [0, 2]
    np.testing.assert_allclose(val, [2.0, 5.0])


def test_merge_opposite_signs_cancel_to_drop_dimension() -> None:
    # +1 与 -1 完全抵消 -> 该维度删除
    idx, val = merge_entries([1, 1], [1.0, -1.0], dim=3)
    assert idx.tolist() == []
    assert val.tolist() == []


def test_merge_partial_cancellation_keeps_residual() -> None:
    idx, val = merge_entries([1, 1, 1], [1.0, 1.0, -0.5], dim=3)
    assert idx.tolist() == [1]
    assert val[0] == pytest.approx(1.5)


def test_merge_output_sorted_and_deduped() -> None:
    idx, val = merge_entries([9, 1, 5, 1], [1.0, 2.0, 3.0, 0.5], dim=10)
    assert idx.tolist() == [1, 5, 9]


def test_create_frozen_vector_is_merged() -> None:
    vec = SparseVector.create([2, 2], [-3.0, 1.0], dim=4)
    assert vec.indices.tolist() == [2]
    assert vec.values.tolist() == [-2.0]
    assert vec.nnz == 1
    assert vec.norm == pytest.approx(2.0)
    with pytest.raises((AttributeError, Exception)):
        vec.dim = 9  # type: ignore[misc]


def test_zero_vector_norm_and_nnz() -> None:
    vec = SparseVector.create([], [], dim=3)
    assert vec.nnz == 0
    assert vec.norm == 0.0
    assert l2_norm(vec.values) == 0.0


def test_rejects_out_of_range_index() -> None:
    with pytest.raises(SparseVectorError):
        merge_entries([3], [1.0], dim=3)
    with pytest.raises(SparseVectorError):
        merge_entries([-1], [1.0], dim=3)


def test_rejects_non_finite_weights() -> None:
    with pytest.raises(SparseVectorError):
        SparseVector.create([0], [float("nan")], dim=2)
    with pytest.raises(SparseVectorError):
        SparseVector.create([0], [float("inf")], dim=2)


def test_rejects_mismatched_lengths() -> None:
    with pytest.raises(SparseVectorError):
        SparseVector.create([0, 1], [1.0], dim=3)


def test_rejects_invalid_dim() -> None:
    with pytest.raises(SparseVectorError):
        SparseVector.create([0], [1.0], dim=0)


def test_parse_entries_form_and_zero_vector() -> None:
    indices, values, dim = parse_entries(
        {"dim": 4, "entries": [{"index": 2, "value": -1.5}]}
    )
    assert (indices, values, dim) == ([2], [-1.5], 4)

    indices, values, dim = parse_entries(
        {"dim": 4, "indices": [1], "values": [2.0]},
        default_dim=8,
    )
    assert (indices, values, dim) == ([1], [2.0], 4)

    indices, values, dim = parse_entries(
        {"entries": []}, default_dim=8
    )
    assert indices == [] and values == [] and dim == 8


def test_parse_entries_validation() -> None:
    with pytest.raises(SparseVectorError):
        parse_entries({"entries": [{"index": "x", "value": 1.0}]})
    with pytest.raises(SparseVectorError):
        parse_entries({"indices": [0], "values": []})
    with pytest.raises(SparseVectorError, match="dim"):
        parse_entries({"entries": []}, require_dim=True)
    with pytest.raises(SparseVectorError):
        parse_entries({"foo": 1})


def test_parse_entries_extra_validation_branches() -> None:
    bad_payloads = [
        "not-a-dict",
        {"dim": 2.5, "entries": []},
        {"dim": -1, "entries": []},
        {"entries": "nope"},
        {"entries": [{"index": 0}]},
        {"entries": ["nope"]},
        {"entries": [{"index": True, "value": 1.0}]},
        {"entries": [{"index": 0, "value": True}]},
        {"indices": "x", "values": []},
        {"indices": [0], "values": "x"},
        {"indices": [0], "values": [1.0, 2.0]},
        {"indices": [True], "values": [1.0]},
        {"indices": [0], "values": [True]},
    ]
    for payload in bad_payloads:
        with pytest.raises(SparseVectorError):
            parse_entries(payload, default_dim=4)


def test_direct_constructor_validation_and_helpers() -> None:
    with pytest.raises(SparseVectorError):
        SparseVector([[0]], [1.0], dim=2)
    with pytest.raises(SparseVectorError):
        SparseVector([0], [1.0], dim=1.5)  # type: ignore[arg-type]
    vec = SparseVector(np.array([0]), np.array([3.0]), dim=2)
    assert vec.to_dict() == {"dim": 2, "indices": [0], "values": [3.0]}

    ni, nv = normalize_entries(np.array([0]), np.array([3.0, 4.0])[:1])
    assert ni.tolist() == [0]


def test_normalize_zero_vector() -> None:
    ni, nv = normalize_entries(np.array([], dtype=np.int64),
                               np.array([], dtype=np.float64))
    assert ni.tolist() == [] and nv.tolist() == []
