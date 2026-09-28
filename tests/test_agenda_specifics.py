"""Agenda ordering, duplicate semantics, refraction and bounded firing."""

from __future__ import annotations

import pytest

from reteapp.core.engine import DUPLICATE_IGNORE, DUPLICATE_MULTISET, DUPLICATE_REJECT
from reteapp.core.errors import FactValidationError, UnknownFactError
from tests.helpers import build_engine
from tests.helpers import load_fixture


def test_agenda_orders_by_salience_then_stable_sequence(review) -> None:
    doc = {
        "rules": [
            {"name": "low", "salience": 0,
             "conditions": [{"type": "T", "constraints": [{"field": "v", "variable": "?v"}]}],
             "action": {}},
            {"name": "high", "salience": 100,
             "conditions": [{"type": "T", "constraints": [{"field": "v", "variable": "?v"}]}],
             "action": {}},
            {"name": "mid", "salience": 50,
             "conditions": [{"type": "T", "constraints": [{"field": "v", "variable": "?v"}]}],
             "action": {}},
        ]
    }
    engine, _rules = build_engine(doc)
    engine.insert_fact({"type": "T", "fields": {"v": 1}})
    ordered = engine.activations()
    assert [a["rule"] for a in ordered] == ["high", "mid", "low"]
    # Stable keys uniquely identify each activation and carry source fact ids.
    assert [a["stable_key"] for a in ordered] == ["high[1]", "mid[1]", "low[1]"]
    review.record(
        "test_agenda_specifics::ordering",
        kind="agenda_order",
        verdict="PASS",
        order=[a["stable_key"] for a in ordered],
    )


def test_equal_salience_keeps_insertion_sequence() -> None:
    doc = {
        "rules": [
            {"name": "r", "salience": 0,
             "conditions": [{"type": "T", "constraints": [{"field": "v", "variable": "?v"}]}],
             "action": {}},
        ]
    }
    engine, _ = build_engine(doc)
    for i in range(4):
        engine.insert_fact({"type": "T", "fields": {"v": i}})
    assert [a["wme_ids"] for a in engine.activations()] == [[1], [2], [3], [4]]


def test_duplicate_facts_default_multiset_coexist_and_join_independently() -> None:
    doc = load_fixture("rules_orders.json")
    engine, _ = build_engine(doc)
    customer = {"type": "Customer", "fields": {"id": "c1", "tier": "gold", "city": "NYC"}}
    first = engine.insert_fact(customer)
    second = engine.insert_fact(customer)
    # Distinct physical WMEs with distinct monotonic ids.
    assert first.wme_id != second.wme_id
    assert first.duplicate is False and second.duplicate is True
    assert len(engine.working_memory()) == 2

    order = {"type": "Order", "fields": {"customer": "c1", "amount": 999}}
    engine.insert_fact(order)
    gold = {tuple(a["wme_ids"]) for a in engine.activations() if a["rule"] == "gold_customer_order"}
    # One order joins EACH duplicate customer -> two physical activations.
    assert gold == {(1, 3), (2, 3)}


def test_duplicate_policy_ignore_keeps_first_and_is_idempotent() -> None:
    doc = {
        "rules": [
            {"name": "r",
             "conditions": [{"type": "T", "constraints": [{"field": "v", "variable": "?v"}]}],
             "action": {}}
        ]
    }
    engine, _ = build_engine(doc, duplicate_policy=DUPLICATE_IGNORE)
    a = engine.insert_fact({"type": "T", "fields": {"v": 5}})
    b = engine.insert_fact({"type": "T", "fields": {"v": 5}})
    assert a.accepted and not b.accepted and b.duplicate
    assert a.wme_id == b.wme_id == 1
    assert len(engine.activations()) == 1  # no phantom second activation


def test_duplicate_policy_reject_raises_explicit_category() -> None:
    doc = {
        "rules": [
            {"name": "r",
             "conditions": [{"type": "T", "constraints": [{"field": "v", "variable": "?v"}]}],
             "action": {}}
        ]
    }
    engine, _ = build_engine(doc, duplicate_policy=DUPLICATE_REJECT)
    engine.insert_fact({"type": "T", "fields": {"v": 5}})
    with pytest.raises(FactValidationError) as exc:
        engine.insert_fact({"type": "T", "fields": {"v": 5}})
    assert exc.value.category == "fact_validation_error"


def test_invalid_facts_rejected_with_category_not_silent_success() -> None:
    doc = {
        "rules": [
            {"name": "r",
             "conditions": [{"type": "T", "constraints": []}],
             "action": {}}
        ]
    }
    engine, _ = build_engine(doc)
    for bad, reason in [
        (["not", "an", "object"], "object"),
        ({"fields": {"x": 1}}, "type"),
        ({"type": "T", "fields": {"nested": {"a": 1}}}, "primitive"),
        ({"type": "T", "fields": {1: "x"}}, "string keys"),
    ]:
        with pytest.raises(FactValidationError, match=reason):
            engine.insert_fact(bad)  # type: ignore[arg-type]


def test_retract_unknown_id_is_explicit_404_category() -> None:
    engine, _ = build_engine({
        "rules": [{"name": "r",
                   "conditions": [{"type": "T", "constraints": []}], "action": {}}]
    })
    with pytest.raises(UnknownFactError) as exc:
        engine.retract_fact(999)
    assert exc.value.category == "unknown_fact_error"


def test_refraction_blocks_refire_until_token_rebuilt() -> None:
    # Rule fires on Counter(n), asserting an identical NEW Counter each time.
    doc = load_fixture("rules_loop.json")
    engine, _ = build_engine(doc, max_fire_rounds=10, refraction=True)
    engine.insert_fact({"type": "Counter", "fields": {"n": 0}})
    first = engine.fire_all()
    # Every firing consumes a DISTINCT physical token (wme 1..10); refraction
    # guarantees none of them can fire a second time.
    fired_keys = [f.stable_key for f in first.fired]
    assert fired_keys == [f"count_up[{i}]" for i in range(1, 11)]
    assert len(set(fired_keys)) == 10
    # The last firing queued one more token; bounded run stopped at the cap.
    assert first.status == "limit_reached"
    assert first.remaining_activations == 1

    # The last firing asserted wme 11, whose activation remains queued; the
    # bounded run stopped rather than looping forever.
    top = engine.agenda.peek_ordered()[0]
    assert top.rule_name == "count_up"
    assert top.token_key == (11,)


def test_retract_rebuild_allows_token_to_fire_again() -> None:
    # Refraction is cleared when the token is destroyed by retraction, so a
    # rebuilt combination over a fresh WME may fire again.
    doc = {
        "rules": [
            {"name": "flag",
             "conditions": [{"type": "Sig", "constraints": [{"field": "k", "variable": "?k"}]}],
             "action": {"assert": [
                 {"type": "Seen", "fields": {"k": {"variable": "k"}}}]},
             "refraction": True}
        ]
    }
    engine, _ = build_engine(doc, max_fire_rounds=10)
    r1 = engine.insert_fact({"type": "Sig", "fields": {"k": "a"}})
    assert engine.fire_next()[1].status == "fired"
    # Re-firing the same alive token is suppressed.
    assert engine.fire_next()[1].status == "agenda_empty"
    engine.retract_fact(r1.wme_id)
    r2 = engine.insert_fact({"type": "Sig", "fields": {"k": "a"}})
    assert r2.wme_id != r1.wme_id
    record, report = engine.fire_next()
    assert report.status == "fired" and record is not None
    assert record.wme_ids == [r2.wme_id]


def test_fire_limit_is_bounded_and_reports_status_not_infinite_loop() -> None:
    doc = load_fixture("rules_loop.json")
    engine, _ = build_engine(doc, max_fire_rounds=3, refraction=False)
    engine.insert_fact({"type": "Counter", "fields": {"n": 0}})
    report = engine.fire_all()
    assert report.status == "limit_reached"
    assert report.rounds_used == 3
    assert report.max_rounds == 3
    assert report.remaining_activations > 0
    # Engine is still consistent and inspectable after the bound.
    assert len(engine.working_memory()) == 4


def test_fire_next_and_step_have_explicit_outcomes() -> None:
    doc = load_fixture("rules_chain.json")
    engine, _ = build_engine(doc, max_fire_rounds=10)
    engine.insert_fact({"type": "Seed", "fields": {"n": 1}})

    record, report = engine.fire_next()
    assert report.status == "fired" and record is not None
    assert record.rule == "seed_to_need"

    stepped = engine.fire_step(2)
    assert [f.rule for f in stepped.fired] == ["need_to_done", "done_final"]

    empty, empty_report = engine.fire_next()
    assert empty is None
    assert empty_report.status == "agenda_empty"
