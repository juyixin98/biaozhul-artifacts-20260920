"""打分与全扫描 TopK 语义测试。"""

from __future__ import annotations

import numpy as np
import pytest

from sparse_retrieval.scoring import brute_force_topk, cosine, sparse_dot
from sparse_retrieval.vector import SparseVector

pytestmark = pytest.mark.unit


def test_sparse_dot_basic_and_negative() -> None:
    a = SparseVector.create([0, 2, 3], [1.0, -2.0, 3.0], dim=5)
    b = SparseVector.create([2, 3, 4], [4.0, 1.0, 10.0], dim=5)
    assert sparse_dot(a, b) == pytest.approx(-8.0 + 3.0)


def test_sparse_dot_disjoint_and_zero() -> None:
    a = SparseVector.create([0], [1.0], dim=3)
    b = SparseVector.create([1], [1.0], dim=3)
    z = SparseVector.create([], [], dim=3)
    assert sparse_dot(a, b) == 0.0
    assert sparse_dot(a, z) == 0.0
    assert sparse_dot(z, z) == 0.0


def test_cosine_known_values() -> None:
    a = SparseVector.create([0, 1], [3.0, 4.0], dim=3)
    b = SparseVector.create([0, 1], [3.0, 4.0], dim=3)
    assert cosine(a, b) == pytest.approx(1.0)
    c = SparseVector.create([0, 1], [-3.0, -4.0], dim=3)
    assert cosine(a, c) == pytest.approx(-1.0)


def test_cosine_with_zero_vector_is_zero_defined() -> None:
    a = SparseVector.create([0], [1.0], dim=2)
    z = SparseVector.create([], [], dim=2)
    assert cosine(a, z) == 0.0
    assert cosine(z, z) == 0.0
    # 不能是 NaN
    assert not np.isnan(cosine(a, z))


def test_topk_tie_break_by_doc_id() -> None:
    docs = [
        SparseVector.create([0], [1.0], dim=2),
        SparseVector.create([0, 1], [1.0, 1.0], dim=2),
        SparseVector.create([0], [2.0], dim=2),  # 与 doc0 同向 -> 并列 1.0
    ]
    q = SparseVector.create([0], [1.0], dim=2)
    got = brute_force_topk(q, docs, k=3)
    assert [d for d, _ in got] == [0, 2, 1]
    assert got[0][1] == pytest.approx(1.0)
    assert got[1][1] == pytest.approx(1.0)


def test_topk_zero_query_returns_lowest_ids() -> None:
    docs = [
        SparseVector.create([0], [1.0], dim=2),
        SparseVector.create([1], [1.0], dim=2),
    ]
    z = SparseVector.create([], [], dim=2)
    got = brute_force_topk(z, docs, k=2)
    assert got == [(0, 0.0), (1, 0.0)]


def test_topk_k_smaller_than_n() -> None:
    docs = [
        SparseVector.create([i], [1.0], dim=5) for i in range(5)
    ]
    docs[4] = SparseVector.create([0, 4], [1.0, 5.0], dim=5)
    q = SparseVector.create([0], [1.0], dim=5)
    got = brute_force_topk(q, docs, k=2)
    assert [d for d, _ in got] == [0, 4]


def test_topk_rejects_bad_k() -> None:
    q = SparseVector.create([0], [1.0], dim=2)
    with pytest.raises(ValueError):
        brute_force_topk(q, [], k=0)


def test_topk_rejects_dim_mismatch() -> None:
    q = SparseVector.create([0], [1.0], dim=2)
    docs = [SparseVector.create([0], [1.0], dim=3)]
    with pytest.raises(ValueError, match="维度"):
        brute_force_topk(q, docs, k=1)
