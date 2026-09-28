"""In-transaction deduplication semantics, cross-checked against mlxtend.

The mlxtend check is an *independent* implementation: we convert raw rows to
the one-hot encoding mlxtend expects and assert our counts and frequent
itemsets agree exactly. Skipped automatically if mlxtend/pandas are absent.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from app.apriori import mine_frequent_itemsets
from app.corpus import build_corpus
from app.indices import TransactionIndex


def _to_onehot(raw: List[List[str]]) -> Any:
    # Mirror our normalization: strip, drop blanks, set-deduplicate, drop empty rows.
    pd = pytest.importorskip("pandas")
    normalized = []
    for row in raw:
        items = {str(x).strip() for x in row if x is not None and str(x).strip()}
        if items:
            normalized.append(items)
    all_items = sorted({i for row in normalized for i in row})
    return pd.DataFrame(
        [{item: (item in row) for item in all_items} for row in normalized]
    )


def test_duplicate_occurrences_collapse_to_one(raw_transactions: List[List[str]]) -> None:
    spec = build_corpus(raw_transactions)
    assert spec.n_transactions == 5
    assert spec.duplicate_occurrences_collapsed == 3
    assert spec.blank_item_tokens_removed == 1
    assert spec.dropped_blank_tids == [5]
    # The first two raw rows normalize to identical sets, so their row counts
    # must be identical even though row 2 lists items multiple times.
    assert spec.transactions[0].items == spec.transactions[1].items


def test_index_counts_ignore_within_row_duplicates(
    raw_transactions: List[List[str]], canonical_data: Dict[str, Any]
) -> None:
    spec = build_corpus(raw_transactions)
    index = TransactionIndex.from_corpus(spec)

    for item, count in canonical_data["singleton_counts"].items():
        assert index.itemset_count((item,)) == count, item

    for pair, count in canonical_data["pair_counts"].items():
        items = tuple(pair.split(","))
        assert index.itemset_count(items) == count, pair

    for triple, count in canonical_data["triple_counts"].items():
        assert index.itemset_count(tuple(triple.split(","))) == count, triple


def test_repeated_item_rows_do_not_inflate_support(raw_transactions: List[List[str]]) -> None:
    # Adding repeated copies of an item inside existing transactions must not
    # change any support count (set semantics).
    inflated = [row + [row[0]] * 5 for row in raw_transactions if row]
    base = build_corpus(raw_transactions)
    inflated_spec = build_corpus(inflated)
    base_idx = TransactionIndex.from_corpus(base)
    inflated_idx = TransactionIndex.from_corpus(inflated_spec)
    for item in base_idx.singleton_items():
        assert inflated_idx.itemset_count((item,)) == base_idx.itemset_count((item,))


@pytest.mark.parametrize("min_support", [0.2, 0.4, 0.6])
def test_apriori_itemsets_match_mlxtend(
    raw_transactions: List[List[str]], min_support: float
) -> None:
    pytest.importorskip("mlxtend")
    pd = pytest.importorskip("pandas")
    from mlxtend.frequent_patterns import apriori as mlx_apriori

    spec = build_corpus(raw_transactions)
    index = TransactionIndex.from_corpus(spec)
    ours = mine_frequent_itemsets(index, min_support)

    onehot = _to_onehot(raw_transactions)
    theirs = mlx_apriori(onehot, min_support=min_support, use_colnames=True)

    ours_by_set = {frozenset(fi.items): fi.support for fi in ours.all()}
    theirs_by_set = {frozenset(row["itemsets"]): float(row["support"]) for _, row in theirs.iterrows()}

    assert set(ours_by_set) == set(theirs_by_set)
    for key, support in ours_by_set.items():
        assert support == pytest.approx(theirs_by_set[key])


def test_empty_corpus_rejected() -> None:
    with pytest.raises(ValueError):
        build_corpus([[], []])


def test_oversized_transaction_rejected() -> None:
    with pytest.raises(ValueError):
        build_corpus([["a", "b", "c"]], max_items_per_transaction=2)
