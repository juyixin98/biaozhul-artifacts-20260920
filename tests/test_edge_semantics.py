"""Additional semantic tests: insertion order independence, transactional
rejection of undefined comparisons, alpha-only rollback, and minor views.

These exercise paths (left activation, predicate-error rollback) that the
main oracle scenarios do not necessarily hit.
"""

from __future__ import annotations

import pytest

from reteapp.core.errors import RuleEvaluationError
from reteapp.lang.predicates import PredicateError  # noqa: F401  (documents origin)
from tests.helpers import build_engine
from tests.test_reference_equivalence import assert_agrees_with_oracle


def test_insertion_order_reversed_matches_oracle_and_uses_left_activations(
    review, monkeypatch
) -> None:
    """Orders inserted before Customers: new depth-1 tokens probe the right
    alpha memory (left activation path). Result must be order-independent."""

    from tests.helpers import load_fixture

    doc = load_fixture("rules_orders.json")
    engine, rules = build_engine(doc)
    # Insert the "right" side first.
    engine.insert_fact({"type": "Order", "fields": {"customer": "c1", "amount": 250}})
    engine.insert_fact({"type": "Order", "fields": {"customer": "c2", "amount": 40}})
    assert_agrees_with_oracle(engine, rules, review,
                              "test_edge_semantics::reversed_order", step="orders_first")
    engine.insert_fact({"type": "Customer", "fields": {"id": "c1", "tier": "gold", "city": "LA"}})
    assert_agrees_with_oracle(engine, rules, review,
                              "test_edge_semantics::reversed_order", step="customer_added")
    engine.insert_fact({"type": "Customer", "fields": {"id": "c2", "tier": "silver", "city": "LA"}})
    assert_agrees_with_oracle(engine, rules, review,
                              "test_edge_semantics::reversed_order", step="all_present")
    pairs = {tuple(a["wme_ids"]) for a in engine.activations()
             if a["rule"] == "customer_order_any_tier"}
    # Physical ordering inside a token follows CE order (Customer, Order),
    # regardless of insertion order.
    assert pairs == {(3, 1), (4, 2)}


def test_undefined_comparison_rejected_transactionally_with_category() -> None:
    doc = {
        "rules": [
            {"name": "r",
             "conditions": [{"type": "T", "constraints": [
                 {"field": "n", "op": ">", "value": 5}]}],
             "action": {}}
        ]
    }
    engine, _ = build_engine(doc)
    before = list(engine.working_memory())
    # String vs number under an ordering operator is undefined: the insert is
    # rejected as rule_evaluation_error, NOT silently treated as no-match.
    with pytest.raises(RuleEvaluationError) as exc:
        engine.insert_fact({"type": "T", "fields": {"n": "abc"}})
    assert exc.value.category == "rule_evaluation_error"
    # Transactional: nothing leaked into working memory or beta memories.
    assert engine.working_memory() == before
    assert len(engine.agenda) == 0
    assert all(m.wmes == {} for m in engine.network.alpha_memories.values())


def test_alpha_constant_rejection_does_not_touch_network() -> None:
    doc = {
        "rules": [
            {"name": "r",
             "conditions": [{"type": "T", "constraints": [
                 {"field": "status", "op": "==", "value": "on"}]}],
             "action": {}}
        ]
    }
    engine, _ = build_engine(doc)
    result = engine.insert_fact({"type": "T", "fields": {"status": "off"}})
    # Accepted into working memory as a WME but present in no alpha memory.
    assert result.accepted
    assert len(engine.working_memory()) == 1
    assert all(m.wmes == {} for m in engine.network.alpha_memories.values())
    assert engine.activations() == []


def test_activation_view_carries_serializable_sources() -> None:
    from tests.helpers import load_fixture

    doc = load_fixture("rules_orders.json")
    engine, _ = build_engine(doc)
    engine.insert_fact({"type": "Customer", "fields": {"id": "c1", "tier": "gold", "city": "NYC"}})
    engine.insert_fact({"type": "Order", "fields": {"customer": "c1", "amount": 250}})
    view = engine.activations()[0]
    rendered = view  # already a plain dict at the engine boundary
    assert set(rendered) >= {"rule", "stable_key", "wme_ids", "bindings", "sources", "sequence"}
    source_types = [s["type"] for s in rendered["sources"]]
    assert source_types == ["Customer", "Order"]


def test_duplicate_wmes_match_oracle_and_retract_one_twin(review) -> None:
    from tests.helpers import load_fixture

    doc = load_fixture("rules_orders.json")
    engine, rules = build_engine(doc)
    customer = {"type": "Customer", "fields": {"id": "c1", "tier": "gold", "city": "NYC"}}
    engine.insert_fact(customer)
    engine.insert_fact(customer)  # distinct physical twin
    engine.insert_fact({"type": "Order", "fields": {"customer": "c1", "amount": 250}})
    assert_agrees_with_oracle(engine, rules, review,
                              "test_edge_semantics::duplicates", step="twin_join")
    gold = {tuple(a["wme_ids"]) for a in engine.activations()
            if a["rule"] == "gold_customer_order"}
    assert gold == {(1, 3), (2, 3)}  # order joins EACH twin

    engine.retract_fact(1)
    assert_agrees_with_oracle(engine, rules, review,
                              "test_edge_semantics::duplicates", step="twin_retracted")
    gold_after = {tuple(a["wme_ids"]) for a in engine.activations()
                  if a["rule"] == "gold_customer_order"}
    assert gold_after == {(2, 3)}


def test_action_retract_invalidates_already_queued_lower_priority_activation() -> None:
    # The high-priority rule retracts the very fact a lower-priority queued
    # activation depends on. The heap entry must be killed mid-fire and the
    # lower rule must NEVER fire on a vanished fact.
    doc = {
        "rules": [
            {"name": "retract_a", "salience": 100,
             "conditions": [{"type": "A", "constraints": [
                 {"field": "k", "variable": "?k"}]}],
             "action": {"retract_ce_indices": [0]}},
            {"name": "use_a", "salience": 0,
             "conditions": [{"type": "A", "constraints": [
                 {"field": "k", "variable": "?k"}]}],
             "action": {"assert": [
                 {"type": "Z", "fields": {"k": {"variable": "k"}}}]}},
        ]
    }
    engine, _ = build_engine(doc, max_fire_rounds=10)
    engine.insert_fact({"type": "A", "fields": {"k": "x"}})
    assert {a["rule"] for a in engine.activations()} == {"retract_a", "use_a"}
    report = engine.fire_all()
    assert [f.rule for f in report.fired] == ["retract_a"]
    assert report.status == "fired"
    assert engine.working_memory() == []


def test_stop_action_halts_firing_with_explicit_status() -> None:
    doc = {
        "rules": [
            {"name": "with_stop", "salience": 10,
             "conditions": [{"type": "A", "constraints": [
                 {"field": "v", "variable": "?v"}]}],
             "action": {"assert": [{"type": "B", "fields": {"v": {"variable": "v"}}}],
                        "stop": True}},
            {"name": "low", "salience": 0,
             "conditions": [{"type": "A", "constraints": [
                 {"field": "v", "variable": "?v"}]}],
             "action": {}},
        ]
    }
    engine, _ = build_engine(doc, max_fire_rounds=10)
    engine.insert_fact({"type": "A", "fields": {"v": 1}})
    report = engine.fire_all()
    assert report.status == "stopped"
    assert [f.rule for f in report.fired] == ["with_stop"]
    # The lower-priority activation is still queued, merely not fired.
    assert [a["rule"] for a in engine.activations()] == ["low"]