"""Inverted index behaviour: TopK stability, zero vectors, updates, pruning."""

import math

import pytest

from sparse_retrieval import InvertedIndex, SparseVector


def make_index():
    index = InvertedIndex()
    index.add(0, SparseVector([(1, 1.0), (2, 1.0)]))
    index.add(1, SparseVector([(1, 1.0)]))
    index.add(2, SparseVector([(2, 1.0), (3, 1.0)]))
    return index


def test_basic_cosine_ranking():
    index = make_index()
    result = index.query(SparseVector([(1, 1.0)]), k=3)
    # doc 1 is identical to the query -> cosine 1.0; doc 0 shares dim 1.
    assert result.results[0] == (1, 1.0)
    assert result.results[1][0] == 0
    assert math.isclose(result.results[1][1], 1.0 / math.sqrt(2.0))
    # doc 2 shares nothing -> cosine exactly 0.0, still returned for k=3.
    assert result.results[2] == (2, 0.0)


def test_candidates_examined_reports_pruned_candidate_set():
    index = make_index()
    result = index.query(SparseVector([(1, 1.0)]), k=2)
    # Only docs 0 and 1 share a dimension with the query; doc 2 is pruned.
    assert result.candidates_examined == 2
    assert result.num_docs == 3


def test_tied_scores_broken_by_doc_id():
    index = InvertedIndex()
    for doc_id in (5, 3, 9):
        index.add(doc_id, SparseVector([(1, 1.0)]))
    result = index.query(SparseVector([(1, 1.0)]), k=3)
    assert [doc_id for doc_id, _ in result.results] == [3, 5, 9]
    assert all(score == 1.0 for _, score in result.results)


def test_negative_weights_produce_negative_scores():
    index = InvertedIndex()
    index.add(0, SparseVector([(1, 1.0)]))
    index.add(1, SparseVector([(1, -1.0)]))
    result = index.query(SparseVector([(1, 1.0)]), k=2)
    assert result.results[0] == (0, 1.0)
    assert result.results[1] == (1, -1.0)


def test_zero_query_vector_scores_everything_zero():
    index = make_index()
    result = index.query(SparseVector([]), k=3)
    assert [score for _, score in result.results] == [0.0, 0.0, 0.0]
    assert [doc_id for doc_id, _ in result.results] == [0, 1, 2]
    assert result.candidates_examined == 0


def test_zero_norm_document_is_stored_but_scores_zero():
    index = InvertedIndex()
    index.add(0, SparseVector([(1, 1.0), (1, -1.0)]))  # cancels to zero
    index.add(1, SparseVector([(1, 1.0)]))
    assert 0 in index
    result = index.query(SparseVector([(1, 1.0)]), k=2)
    assert result.results[0] == (1, 1.0)
    assert result.results[1] == (0, 0.0)
    # The zero vector posts to no list, so it is not even a candidate.
    assert result.candidates_examined == 1


def test_k_larger_than_candidate_set_fills_with_zero_scores():
    index = make_index()
    result = index.query(SparseVector([(1, 1.0)]), k=10)
    assert len(result.results) == 3  # clamped to num_docs
    assert result.results[-1] == (2, 0.0)


def test_remove_document():
    index = make_index()
    assert index.remove(1) is True
    assert index.remove(1) is False
    result = index.query(SparseVector([(1, 1.0)]), k=3)
    assert [doc_id for doc_id, _ in result.results] == [0, 2]


def test_add_replaces_existing_document():
    index = make_index()
    index.add(1, SparseVector([(3, 1.0)]))  # doc 1 no longer has dim 1
    result = index.query(SparseVector([(1, 1.0)]), k=3)
    assert [doc_id for doc_id, _ in result.results] == [0, 1, 2]
    assert result.results[0][1] > 0.0
    assert result.results[1][1] == 0.0
    assert result.candidates_examined == 1


def test_query_dimension_absent_from_index_is_skipped():
    index = make_index()
    result = index.query(SparseVector([(1, 1.0), (999, 5.0)]), k=1)
    # qnorm = sqrt(1 + 25); doc 1 has dot 1.0 and norm 1.0.
    assert result.results[0][0] == 1
    assert math.isclose(result.results[0][1], 1.0 / math.sqrt(26.0))
    # dim 999 hits no posting list; candidates unchanged.
    assert result.candidates_examined == 2


def test_invalid_arguments_rejected():
    index = make_index()
    with pytest.raises(ValueError):
        index.query(SparseVector([(1, 1.0)]), k=0)
    with pytest.raises(ValueError):
        index.add(-1, SparseVector([(1, 1.0)]))
    with pytest.raises(ValueError):
        index.add("x", SparseVector([(1, 1.0)]))
    with pytest.raises(ValueError):
        index.add(7, {"1": 1.0})
