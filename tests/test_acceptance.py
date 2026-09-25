"""Acceptance: inverted index vs full scan on random sparse data.

Covers negative weights, duplicate dimensions, tied scores, zero vectors,
and verifies the reported candidate counts. Index results must equal the
full scan *bitwise* (same accumulation order), not merely approximately.
"""

import numpy as np
import pytest

from sparse_retrieval import InvertedIndex, SparseVector, brute_force_query
from sparse_retrieval.synthetic import random_corpus, random_queries, to_dense

DIM = 500
NUM_DOCS = 400
NUM_QUERIES = 30
K = 15


def _build_index(docs):
    index = InvertedIndex()
    for doc_id, vec in docs.items():
        index.add(doc_id, vec)
    return index


@pytest.mark.parametrize("seed", [0, 1, 2, 7, 42])
def test_index_matches_full_scan_on_random_sparse_data(seed):
    docs = random_corpus(seed=seed, num_docs=NUM_DOCS, dim=DIM)
    queries = random_queries(seed=1000 + seed, num_queries=NUM_QUERIES, dim=DIM)
    index = _build_index(docs)

    for q in queries:
        indexed = index.query(q, k=K)
        scanned = brute_force_query(docs, q, k=K)
        # Bitwise equality: identical doc order and identical float scores.
        assert indexed.results == scanned.results
        assert indexed.num_docs == scanned.num_docs == NUM_DOCS
        # The index must genuinely prune: candidates <= full scan.
        assert indexed.candidates_examined <= scanned.candidates_examined


def test_index_matches_numpy_dense_reference():
    """Cross-check scores against a NumPy dense matrix cosine."""
    docs = random_corpus(seed=123, num_docs=200, dim=300)
    queries = random_queries(seed=999, num_queries=10, dim=300)
    index = _build_index(docs)

    doc_ids = sorted(docs)
    matrix = np.stack([to_dense(docs[i], 300) for i in doc_ids])
    norms = np.linalg.norm(matrix, axis=1)

    for q in queries:
        qv = to_dense(q, 300)
        qn = np.linalg.norm(qv)
        denom = norms * qn
        with np.errstate(divide="ignore", invalid="ignore"):
            dense_scores = np.where(denom > 0.0, (matrix @ qv) / denom, 0.0)

        indexed = index.query(q, k=K)
        for doc_id, score in indexed.results:
            dense_score = dense_scores[doc_ids.index(doc_id)]
            assert np.isclose(score, dense_score, rtol=1e-9, atol=1e-12)


def test_negative_weights_included_in_random_equivalence():
    # All-negative docs vs all-positive queries -> every shared dimension
    # contributes a negative product, so negative cosines are guaranteed.
    docs = random_corpus(seed=5, num_docs=100, dim=200, negative_ratio=1.0)
    queries = random_queries(seed=6, num_queries=10, dim=200, negative_ratio=0.0)
    index = _build_index(docs)
    saw_negative = False
    for q in queries:
        indexed = index.query(q, k=20)
        scanned = brute_force_query(docs, q, k=20)
        assert indexed.results == scanned.results
        saw_negative |= any(score < 0.0 for _, score in indexed.results)
    assert saw_negative, "expected negative cosine scores to be exercised"


def test_duplicate_dimensions_in_input_are_merged_before_indexing():
    # Raw pairs with duplicates, fed through SparseVector (the merge point).
    docs = {
        0: SparseVector([(1, 0.5), (1, 0.5), (2, 1.0)]),   # -> {1: 1.0, 2: 1.0}
        1: SparseVector([(1, 1.0), (2, 1.0)]),             # identical merged
        2: SparseVector([(1, 1.0), (1, -1.0), (3, 2.0)]),  # -> {3: 2.0}
    }
    index = _build_index(docs)
    q = SparseVector([(1, 1.0), (2, 1.0)])
    indexed = index.query(q, k=3)
    scanned = brute_force_query(docs, q, k=3)
    assert indexed.results == scanned.results
    # Docs 0 and 1 are identical after merging -> bitwise-tied score, doc_id
    # order. (sqrt(2)*sqrt(2) != 2.0 in float, so the cosine is 1.0 - eps.)
    assert [d for d, _ in indexed.results[:2]] == [0, 1]
    assert indexed.results[0][1] == indexed.results[1][1]
    assert np.isclose(indexed.results[0][1], 1.0)


def test_tied_scores_have_stable_doc_id_order():
    docs = {doc_id: SparseVector([(1, 1.0), (2, 1.0)]) for doc_id in (40, 4, 17)}
    index = _build_index(docs)
    q = SparseVector([(1, 1.0), (2, 1.0)])
    for k in (1, 2, 3):
        indexed = index.query(q, k=k)
        scanned = brute_force_query(docs, q, k=k)
        assert indexed.results == scanned.results
        assert [d for d, _ in indexed.results] == [4, 17, 40][:k]


def test_zero_vectors_in_corpus_and_query():
    docs = random_corpus(seed=11, num_docs=50, dim=100)
    docs[1000] = SparseVector([])                      # empty zero vector
    docs[1001] = SparseVector([(3, 1.0), (3, -1.0)])   # cancels to zero
    index = _build_index(docs)

    # Zero query: everything scores 0.0, order by doc_id.
    zero_q = SparseVector([])
    indexed = index.query(zero_q, k=52)
    scanned = brute_force_query(docs, zero_q, k=52)
    assert indexed.results == scanned.results
    assert all(score == 0.0 for _, score in indexed.results)
    assert indexed.candidates_examined == 0

    # Normal query: zero-norm docs score 0.0 and rank last (highest ids).
    q = random_queries(seed=12, num_queries=1, dim=100)[0]
    indexed = index.query(q, k=52)
    scanned = brute_force_query(docs, q, k=52)
    assert indexed.results == scanned.results
    assert dict(indexed.results)[1000] == 0.0
    assert dict(indexed.results)[1001] == 0.0


def test_candidate_counts_are_reported_and_exact():
    docs = random_corpus(seed=21, num_docs=NUM_DOCS, dim=DIM)
    index = _build_index(docs)
    queries = random_queries(seed=22, num_queries=NUM_QUERIES, dim=DIM)
    for q in queries:
        result = index.query(q, k=K)
        # Independently recount candidates: docs sharing >= 1 query dim.
        q_dims = {d for d, _ in q.entries}
        expected = sum(
            1 for vec in docs.values() if any(d in q_dims for d, _ in vec.entries)
        )
        assert result.candidates_examined == expected
