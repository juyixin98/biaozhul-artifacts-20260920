"""Rule generation: hand-computed metrics, warnings, no-causation flags."""
from __future__ import annotations

import pytest

from app.mining.itemsets import mine_frequent_itemsets
from app.mining.rules import (
    W_HIGH_CONF_LOW_LIFT,
    W_RARE_EVENT,
    W_SMALL_SAMPLE,
    W_UBIQUITOUS_CONSEQUENT,
    attach_warnings,
    generate_rules,
)
from tests.conftest import fixture_transactions
from tests.hand_computed import (
    ALPHA_TO_BETA,
    APPLE_TO_BASE,
    BASIC_RULES_MINCONF_0_8,
    BEER_TO_DIAPERS,
    MILK_DIAPERS_TO_BEER,
    RAREA_TO_RAREB,
)


def _counts(name: str, min_support: float):
    txs = fixture_transactions(name)
    itemsets, n = mine_frequent_itemsets(txs, min_support)
    return {fi.items: fi.support_count for fi in itemsets}, n


def _find(rules, ante, cons):
    for r in rules:
        if r.antecedent == frozenset(ante) and r.consequent == frozenset(cons):
            return r
    raise AssertionError(f"rule {ante} -> {cons} not generated")


def _assert_metrics(rule, expected):
    assert rule.metrics.support == pytest.approx(float(expected["support"]))
    assert rule.metrics.confidence == pytest.approx(float(expected["confidence"]))
    assert rule.metrics.lift == pytest.approx(float(expected["lift"]))
    assert rule.metrics.leverage == pytest.approx(float(expected["leverage"]))


def test_beer_to_diapers_hand_computed():
    counts, n = _counts("basic", 0.6)
    rules = generate_rules(counts, n, min_confidence=0.7)
    _assert_metrics(_find(rules, ["beer"], ["diapers"]), BEER_TO_DIAPERS)


def test_milk_diapers_to_beer_hand_computed():
    counts, n = _counts("basic", 0.4)
    rules = generate_rules(counts, n, min_confidence=0.6)
    _assert_metrics(_find(rules, ["milk", "diapers"], ["beer"]), MILK_DIAPERS_TO_BEER)


def test_min_confidence_pruning_keeps_exactly_expected_rules():
    counts, n = _counts("basic", 0.6)
    rules = generate_rules(counts, n, min_confidence=0.8)
    got = sorted((r.antecedent, r.consequent) for r in rules)
    assert got == BASIC_RULES_MINCONF_0_8


def test_min_lift_filter_applies_after_confidence():
    counts, n = _counts("basic", 0.6)
    # lift 1.25 for beer->diapers; threshold 1.3 must drop everything
    assert generate_rules(counts, n, min_confidence=0.7, min_lift=1.3) == []
    kept = generate_rules(counts, n, min_confidence=0.7, min_lift=1.2)
    assert all(r.metrics.lift >= 1.2 for r in kept)
    assert _find(kept, ["beer"], ["diapers"]) is not None


def test_ubiquitous_consequent_high_confidence_is_not_causal(service, load_corpus):
    """confidence 1.0 with lift 1.0 must be flagged, not celebrated."""
    corpus_id = load_corpus("ubiquitous")
    service.mine(corpus_id, 0.5)
    rules = service.generate_rules(corpus_id, min_confidence=0.9, min_lift=None)
    rule = _find(rules, ["apple"], ["base"])
    _assert_metrics(rule, APPLE_TO_BASE)
    assert W_HIGH_CONF_LOW_LIFT in rule.warnings
    assert W_UBIQUITOUS_CONSEQUENT in rule.warnings


def test_mutually_exclusive_items_negative_leverage(service, load_corpus):
    corpus_id = load_corpus("exclusive")
    rule = service.evaluate(corpus_id, ["alpha"], ["beta"])
    _assert_metrics(rule, ALPHA_TO_BETA)
    assert W_HIGH_CONF_LOW_LIFT not in rule.warnings  # confidence is 0, not high


def test_rare_event_warning(service, load_corpus):
    corpus_id = load_corpus("rare_combo")
    service.mine(corpus_id, 0.01)
    rules = service.generate_rules(corpus_id, min_confidence=0.5, min_lift=None)
    rule = _find(rules, ["rareA"], ["rareB"])
    _assert_metrics(rule, RAREA_TO_RAREB)
    assert W_RARE_EVENT in rule.warnings
    assert W_SMALL_SAMPLE not in rule.warnings  # N=40 >= threshold


def test_small_sample_warning(service, load_corpus):
    corpus_id = load_corpus("basic")  # N=5 < default threshold 30
    service.mine(corpus_id, 0.6)
    rules = service.generate_rules(corpus_id, min_confidence=0.7, min_lift=None)
    assert rules
    assert all(W_SMALL_SAMPLE in r.warnings for r in rules)


def test_attach_warnings_is_deterministic():
    from app.mining.rules import Rule, compute_metrics

    rule = Rule(frozenset(["a"]), frozenset(["b"]), compute_metrics(1, 1, 1, 2))
    attach_warnings(
        rule,
        n_transactions=2,
        joint_count=1,
        consequent_support=0.5,
        small_sample_threshold=30,
        rare_event_count_threshold=5,
        ubiquitous_support_threshold=0.9,
    )
    assert rule.warnings == [W_SMALL_SAMPLE, W_RARE_EVENT]
