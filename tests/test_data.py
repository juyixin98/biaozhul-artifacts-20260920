"""合成数据生成: 可复现性与覆盖性测试。"""

from __future__ import annotations

import numpy as np
import pytest

from sparse_retrieval.data import generate_dataset

pytestmark = pytest.mark.unit


def test_seed_reproducible() -> None:
    a = generate_dataset(seed=123)
    b = generate_dataset(seed=123)
    assert a.dim == b.dim and len(a.docs) == len(b.docs)
    for da, db in zip(a.docs, b.docs):
        assert da.indices.tolist() == db.indices.tolist()
        np.testing.assert_array_equal(da.values, db.values)


def test_dataset_covers_zero_and_negative_and_duplicates() -> None:
    ds = generate_dataset(
        seed=1, n_docs=400, dim=100, n_zero_docs=10, negative_prob=0.5,
        duplicate_prob=1.0,
    )
    zero_count = sum(1 for d in ds.docs if d.nnz == 0)
    assert zero_count == 10

    neg_count = sum(int(np.sum(d.values < 0)) for d in ds.docs)
    assert neg_count > 0

    # 合并保证所有文档维度去重
    for d in ds.docs:
        assert d.indices.size == np.unique(d.indices).size


def test_dataset_dimensions_consistent() -> None:
    ds = generate_dataset(seed=2, n_docs=50, dim=32, n_queries=5)
    assert all(d.dim == 32 for d in ds.docs)
    assert all(q.dim == 32 for q in ds.queries)
    assert len(ds.queries) == 5
