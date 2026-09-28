"""Frequent itemset mining against hand-computed and independent references."""
from __future__ import annotations

from fractions import Fraction

from app.mining.itemsets import mine_frequent_itemsets
from tests.conftest import fixture_transactions
from tests.hand_computed import BASIC_FREQUENT_0_6, DUPLICATES_SUPPORT
from tests.reference_impl import all_frequent_itemsets


def _as_counts(itemsets):
    return {fi.items: fi.support_count for fi in itemsets}


def test_basic_itemsets_match_hand_computed():
    txs = fixture_transactions("basic")
    itemsets, n = mine_frequent_itemsets(txs, 0.6)
    assert n == 5
    assert _as_counts(itemsets) == BASIC_FREQUENT_0_6


def test_duplicates_fixture_counts_match_hand_computed():
    txs = fixture_transactions("duplicates")
    itemsets, n = mine_frequent_itemsets(txs, 1 / 3)
    assert n == 3
    assert _as_counts(itemsets) == DUPLICATES_SUPPORT


def test_support_ratio_and_count_are_consistent():
    txs = fixture_transactions("basic")
    itemsets, n = mine_frequent_itemsets(txs, 0.6)
    for fi in itemsets:
        assert fi.support == fi.support_count / n


def test_min_support_boundary_is_inclusive():
    # support exactly 0.6 (3/5) must be kept
    txs = fixture_transactions("basic")
    itemsets, _ = mine_frequent_itemsets(txs, 0.6)
    assert frozenset(["diapers", "beer"]) in _as_counts(itemsets)
    # just above 0.6 it must be dropped
    itemsets, _ = mine_frequent_itemsets(txs, 0.61)
    assert frozenset(["diapers", "beer"]) not in _as_counts(itemsets)


def test_invalid_min_support_rejected():
    for bad in (0.0, -0.1, 1.01):
        try:
            mine_frequent_itemsets([frozenset(["a"])], bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"min_support={bad} should be rejected")


def test_candidates_are_not_duplicated():
    # regression: different pairs can share a union; each itemset must
    # appear exactly once in the output
    txs = fixture_transactions("basic")
    itemsets, _ = mine_frequent_itemsets(txs, 0.4)
    keys = [fi.items for fi in itemsets]
    assert len(keys) == len(set(keys))


def test_remining_replaces_itemsets_idempotently(service, load_corpus):
    corpus_id = load_corpus("basic")
    service.mine(corpus_id, 0.6)
    service.generate_rules(corpus_id, 0.7, None)
    service.mine(corpus_id, 0.4)  # must not raise IntegrityError
    counts = service._itemset_counts(corpus_id)
    assert counts[frozenset(["milk", "diapers", "beer"])] == 2


def test_matches_independent_bruteforce_reference():
    """Cross-check the Apriori kernel against the independent powerset
    reference implementation on every fixture and several thresholds."""
    for name in ("basic", "ubiquitous", "exclusive", "rare_combo", "duplicates"):
        txs = fixture_transactions(name)
        for threshold in (Fraction(1, 5), Fraction(2, 5), Fraction(3, 5), Fraction(1, 1)):
            expected = all_frequent_itemsets(txs, threshold)
            got, _ = mine_frequent_itemsets(txs, float(threshold))
            assert _as_counts(got) == expected, f"{name} @ {threshold}"
