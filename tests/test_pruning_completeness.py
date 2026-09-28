"""Min-confidence pruning must not lose rules.

Compares the pruned generator against (a) the core's own exhaustive
enumerator and (b) the independent Fraction-based reference, across all
fixtures and a sweep of confidence thresholds.
"""
from __future__ import annotations

from fractions import Fraction

from app.mining.itemsets import mine_frequent_itemsets
from app.mining.rules import all_rules_bruteforce, generate_rules
from tests.conftest import fixture_transactions
from tests.reference_impl import all_rules as reference_rules


def _keyed(rules):
    return sorted((sorted(r.antecedent), sorted(r.consequent)) for r in rules)


def test_pruned_generation_matches_exhaustive_and_reference():
    for name in ("basic", "ubiquitous", "exclusive", "rare_combo", "duplicates"):
        txs = fixture_transactions(name)
        for min_support in (0.2, 0.4, 0.6):
            itemsets, n = mine_frequent_itemsets(txs, min_support)
            counts = {fi.items: fi.support_count for fi in itemsets}
            for min_conf in (0.3, 0.5, 0.7, 0.8, 0.9, 1.0):
                pruned = generate_rules(counts, n, min_conf)
                exhaustive = all_rules_bruteforce(counts, n, min_conf)
                assert _keyed(pruned) == _keyed(exhaustive), (
                    f"{name} ms={min_support} mc={min_conf}: pruning lost rules"
                )

                frequent = {k: v for k, v in counts.items()}
                ref = reference_rules(txs, frequent, Fraction(min_conf))
                ref_keyed = sorted((sorted(a), sorted(c)) for a, c in ref)
                assert _keyed(pruned) == ref_keyed, (
                    f"{name} ms={min_support} mc={min_conf}: mismatch vs reference"
                )


def test_pruned_metrics_match_reference_exactly():
    txs = fixture_transactions("basic")
    itemsets, n = mine_frequent_itemsets(txs, 0.4)
    counts = {fi.items: fi.support_count for fi in itemsets}
    from tests.reference_impl import metrics as ref_metrics

    for rule in generate_rules(counts, n, 0.5):
        ref = ref_metrics(txs, rule.antecedent, rule.consequent)
        assert rule.metrics.confidence == float(ref["confidence"])
        assert rule.metrics.lift == float(ref["lift"])
        assert rule.metrics.leverage == float(ref["leverage"])
        assert rule.metrics.support == float(ref["support"])
