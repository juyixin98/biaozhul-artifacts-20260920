"""Corpus specification: normalization, dedup semantics, rejection categories."""
from __future__ import annotations

import pytest

from app.corpus.schema import CorpusError, normalize_corpus, normalize_transaction
from tests.hand_computed import DUPLICATES_REMOVED, DUPLICATES_SUPPORT


def test_duplicates_within_transaction_are_collapsed():
    norm = normalize_transaction("D1", ["x", "x", "y", "y", "y"])
    assert norm.items == frozenset({"x", "y"})
    assert norm.n_items_raw == 5
    assert norm.n_duplicates_removed == 3


def test_whitespace_is_stripped_before_dedup():
    norm = normalize_transaction("T", [" a ", "a", "b"])
    assert norm.items == frozenset({"a", "b"})
    assert norm.n_duplicates_removed == 1


@pytest.mark.parametrize(
    "tid, items, category",
    [
        ("", ["a"], "EMPTY_TRANSACTION_ID"),
        ("  ", ["a"], "EMPTY_TRANSACTION_ID"),
        ("T", [], "EMPTY_TRANSACTION"),
        ("T", ["", "  "], "EMPTY_ITEM"),
        ("T", ["x" * 300], "ITEM_TOO_LONG"),
    ],
)
def test_invalid_transactions_rejected_with_category(tid, items, category):
    with pytest.raises(CorpusError) as excinfo:
        normalize_transaction(tid, items)
    assert excinfo.value.category == category


def test_duplicate_transaction_ids_rejected():
    with pytest.raises(CorpusError) as excinfo:
        normalize_corpus([("T1", ["a"]), ("T1", ["b"])])
    assert excinfo.value.category == "DUPLICATE_TRANSACTION_ID"


def test_empty_corpus_rejected():
    with pytest.raises(CorpusError) as excinfo:
        normalize_corpus([])
    assert excinfo.value.category == "EMPTY_CORPUS"


def test_dedup_matches_set_semantics_of_support_libraries(service):
    """The duplicates fixture must yield exactly the support counts a
    set-based library (mlxtend one-hot / efficient-apriori) produces."""
    corpus_id = service.create_corpus(
        "dup",
        [("D1", ["x", "x", "y", "y", "y"]), ("D2", ["x", "z"]), ("D3", ["y", "z", "z"])],
    ).corpus_id
    for items, expected in DUPLICATES_SUPPORT.items():
        assert service.store.support_count(corpus_id, items) == expected


def test_ingest_reports_removed_duplicates(service):
    loaded = service.create_corpus(
        "dup",
        [("D1", ["x", "x", "y", "y", "y"]), ("D2", ["x", "z"]), ("D3", ["y", "z", "z"])],
    )
    assert loaded.n_duplicate_items_removed == DUPLICATES_REMOVED
    assert loaded.n_transactions == 3
    assert loaded.n_distinct_items == 3
