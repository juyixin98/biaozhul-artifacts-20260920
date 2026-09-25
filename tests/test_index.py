"""倒排索引 + WAND 剪枝测试: 精确性、候选数与边界语义。"""

from __future__ import annotations

import numpy as np
import pytest

from sparse_retrieval.index import InvertedIndex, IndexError
from sparse_retrieval.scoring import brute_force_topk
from sparse_retrieval.vector import SparseVector

from .conftest import random_sparse

pytestmark = pytest.mark.unit


def _build_random(rng: np.random.Generator, n: int = 300, dim: int = 80):
    docs = [random_sparse(rng, dim, int(rng.integers(1, 18))) for _ in range(n)]
    # 插入零向量文档与重复维度文档(构造时合并)
    docs.append(SparseVector.create([], [], dim))
    docs.append(SparseVector.create([1, 1, 2], [1.0, -1.0, 3.0], dim))
    return docs


@pytest.mark.parametrize("seed", range(20))
def test_matches_brute_force_random(seed: int) -> None:
    rng = np.random.default_rng(1000 + seed)
    dim = 64
    docs = _build_random(rng, n=250, dim=dim)
    index = InvertedIndex(docs)
    for _ in range(15):
        q = random_sparse(rng, dim, int(rng.integers(1, 12)))
        for k in (1, 5, 20, 100):
            got = [(h.doc_id, h.score) for h in index.search(q, k=k).hits]
            expect = brute_force_topk(q, docs, k)
            assert got == expect, (seed, k)


@pytest.mark.parametrize("seed", range(10))
def test_matches_brute_force_zero_query(seed: int) -> None:
    rng = np.random.default_rng(2000 + seed)
    dim = 30
    docs = _build_random(rng, n=80, dim=dim)
    index = InvertedIndex(docs)
    z = SparseVector.create([], [], dim)
    for k in (1, 10, 200):
        got = [(h.doc_id, h.score) for h in index.search(z, k=k).hits]
        assert got == brute_force_topk(z, docs, k)


def test_query_with_unseen_terms_all_zero() -> None:
    docs = [SparseVector.create([0], [1.0], dim=3) for _ in range(4)]
    index = InvertedIndex(docs)
    q = SparseVector.create([2], [1.0], dim=3)
    got = [(h.doc_id, h.score) for h in index.search(q, k=3).hits]
    assert got == [(0, 0.0), (1, 0.0), (2, 0.0)]


def test_negative_weights_exact() -> None:
    docs = [
        SparseVector.create([0, 1], [1.0, 1.0], dim=2),
        SparseVector.create([0], [-1.0], dim=2),
    ]
    index = InvertedIndex(docs)
    q = SparseVector.create([0], [1.0], dim=2)
    got = [(h.doc_id, round(h.score, 12)) for h in index.search(q, k=2).hits]
    # doc0: 1/sqrt2; doc1: -1
    assert got == [(0, round(1.0 / np.sqrt(2.0), 12)), (1, -1.0)]


def test_duplicate_dimensions_merged_before_indexing() -> None:
    # 重复维度 + 异号抵消; 合并后与等价单一条目文档检索结果一致
    docs = [
        SparseVector.create([0, 0, 1], [2.0, 3.0, -4.0], dim=3),
        SparseVector.create([0, 1], [5.0, -4.0], dim=3),
    ]
    index = InvertedIndex(docs)
    # 两个文档合并后完全相同 -> 与自身查询余弦都是 1.0, ID 升序
    q = SparseVector.create([0, 1], [5.0, -4.0], dim=3)
    got = [(h.doc_id, round(h.score, 12)) for h in index.search(q, k=2).hits]
    assert got == [(0, 1.0), (1, 1.0)]
    # 倒排链每维每文档只出现一次(注意 postings[t] = (doc_ids, weights))
    for doc_ids, _ in index.postings.values():
        assert len(np.unique(doc_ids)) == len(doc_ids)


def test_ties_resolved_by_doc_id_under_pruning() -> None:
    # 大量同分文档, 验证剪枝路径上的并列仍然稳定
    docs = [SparseVector.create([0], [1.0], dim=2) for _ in range(20)]
    docs += [SparseVector.create([0, 1], [1.0, 1.0], dim=2) for _ in range(20)]
    index = InvertedIndex(docs)
    q = SparseVector.create([0], [1.0], dim=2)
    got = [h.doc_id for h in index.search(q, k=15).hits]
    expect = [d for d, _ in brute_force_topk(q, docs, 15)]
    assert got == expect


def test_candidate_counts_reported_and_pruned() -> None:
    rng = np.random.default_rng(42)
    dim = 200
    docs = [random_sparse(rng, dim, 15) for _ in range(1000)]
    index = InvertedIndex(docs)
    q = random_sparse(rng, dim, 8)
    result = index.search(q, k=10)
    stats = result.stats_dict()
    # 候选数不超过全扫描, 且本配置下确实发生剪枝
    assert stats["candidates_scored"] <= stats["n_docs"]
    assert stats["wand_pivot_skips"] > 0
    assert stats["candidates_scored"] < stats["n_docs"]
    # 返回精确结果
    expect = brute_force_topk(q, docs, 10)
    assert [(h.doc_id, h.score) for h in result.hits] == expect


def test_k_larger_than_corpus() -> None:
    docs = [SparseVector.create([0], [1.0], dim=2)]
    index = InvertedIndex(docs)
    result = index.search(SparseVector.create([0], [1.0], 2), k=10)
    assert len(result.hits) == 1
    assert result.stats_dict()["candidates_considered"] == 1


def test_rejects_dim_mismatch_and_bad_k() -> None:
    index = InvertedIndex([SparseVector.create([0], [1.0], dim=2)])
    with pytest.raises(IndexError):
        index.search(SparseVector.create([0], [1.0], dim=3), k=2)
    with pytest.raises(IndexError):
        index.search(SparseVector.create([0], [1.0], dim=2), k=0)


def test_empty_corpus_rejected() -> None:
    with pytest.raises(IndexError):
        InvertedIndex([])


def test_mixed_dim_corpus_rejected() -> None:
    with pytest.raises(IndexError):
        InvertedIndex(
            [
                SparseVector.create([0], [1.0], dim=2),
                SparseVector.create([0], [1.0], dim=3),
            ]
        )


@pytest.mark.parametrize("seed", range(5))
def test_upper_bound_safe_under_negative_terms(seed: int) -> None:
    """高负权重 + 高正权重混合: 剪枝不得丢掉正分赢家。"""
    rng = np.random.default_rng(3000 + seed)
    dim = 40
    docs = []
    for _ in range(150):
        idx0 = np.array([0, int(rng.integers(1, dim))])
        val0 = np.where(rng.random(2) < 0.5, -1.0, 1.0) * rng.uniform(0.1, 5, 2)
        docs.append(SparseVector.create(idx0.tolist(), val0.tolist(), dim))
    index = InvertedIndex(docs)
    q = SparseVector.create([0], [1.0], dim)
    for k in (1, 5, 50, 150):
        got = [(h.doc_id, h.score) for h in index.search(q, k).hits]
        assert got == brute_force_topk(q, docs, k)
