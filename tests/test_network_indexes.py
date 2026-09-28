"""Structural tests for alpha/beta memories and their indexes.

These assert the architecture the review asks for - indexed alpha and beta
memories - rather than only externally observable matches.
"""

from __future__ import annotations

from tests.helpers import build_engine


def test_alpha_memory_is_shared_between_rules_with_same_constant_tests() -> None:
    doc = {
        "rules": [
            {"name": "r1", "salience": 0,
             "conditions": [{"type": "E", "constraints": [
                 {"field": "dept", "variable": "?d"},
                 {"field": "active", "op": "==", "value": True}]}],
             "action": {}},
            {"name": "r2", "salience": 0,
             "conditions": [{"type": "E", "constraints": [
                 {"field": "active", "op": "==", "value": True},
                 {"field": "dept", "variable": "?d"}]}],
             "action": {}},
        ]
    }
    engine, _ = build_engine(doc)
    # Same constant-test SET (order independent) -> one shared alpha memory.
    employee_memories = [
        m for m in engine.network.alpha_memories.values() if m.fact_type == "E"
    ]
    assert len(employee_memories) == 1


def test_alpha_memory_indexes_join_fields_and_membership() -> None:
    doc = {
        "rules": [
            {"name": "join_rule", "salience": 0,
             "conditions": [
                 {"type": "C", "constraints": [{"field": "id", "variable": "?cid"}]},
                 {"type": "O", "constraints": [{"field": "customer", "variable": "?cid"}]},
             ],
             "action": {}}
        ]
    }
    engine, _ = build_engine(doc)
    engine.insert_fact({"type": "C", "fields": {"id": "c1"}})
    engine.insert_fact({"type": "C", "fields": {"id": "c2"}})

    order_memory = next(m for m in engine.network.alpha_memories.values() if m.fact_type == "O")
    # Right-side alpha memory maintains a value index on the join field.
    assert "customer" in order_memory.field_index
    engine.insert_fact({"type": "O", "fields": {"customer": "c1"}})
    assert order_memory.field_index["customer"]["c1"] == {3}

    # Beta memory at depth 1 indexes tokens by the bound variable.
    beta1 = engine.network.beta_memories[("join_rule", 0)]
    assert set(beta1.var_index["cid"].keys()) == {"c1", "c2"}
    # Beta memory indexes tokens by contained WME for retract propagation.
    assert set(beta1.wme_index.keys()) == {1, 2}


def test_beta_wme_index_drives_full_retract_cascade_across_depths() -> None:
    doc = {
        "rules": [
            {"name": "chain3", "salience": 0,
             "conditions": [
                 {"type": "A", "constraints": [{"field": "k", "variable": "?k"}]},
                 {"type": "B", "constraints": [{"field": "k", "variable": "?k"}]},
                 {"type": "C", "constraints": [{"field": "k", "variable": "?k"}]},
             ],
             "action": {}}
        ]
    }
    engine, _ = build_engine(doc)
    engine.insert_fact({"type": "A", "fields": {"k": "x"}})
    engine.insert_fact({"type": "B", "fields": {"k": "x"}})
    engine.insert_fact({"type": "C", "fields": {"k": "x"}})

    terminal = engine.network.beta_memories[("chain3", 2)]
    assert set(terminal.tokens) == {(1, 2, 3)}

    # Retract the middle WME: depth-2 and depth-3 dependent tokens all go.
    engine.retract_fact(2)
    assert engine.network.beta_memories[("chain3", 1)].tokens == {}
    assert terminal.tokens == {}
    # Depth-1 token for the surviving A is untouched.
    assert set(engine.network.beta_memories[("chain3", 0)].tokens) == {(1,)}
    assert len(engine.agenda) == 0


def test_network_digest_reports_indexes() -> None:
    doc = {
        "rules": [
            {"name": "r", "salience": 0,
             "conditions": [
                 {"type": "A", "constraints": [{"field": "k", "variable": "?k"}]},
                 {"type": "B", "constraints": [{"field": "k", "variable": "?k"}]},
             ],
             "action": {}}
        ]
    }
    engine, _ = build_engine(doc)
    engine.insert_fact({"type": "A", "fields": {"k": "v"}})
    digest = engine.digest()
    alpha_types = {m["type"]: m for m in digest["alpha_memories"]}
    assert alpha_types["A"]["members"] == 1
    assert "k" in alpha_types["B"]["field_indexes"]
    beta = {(m["rule"], m["ce_index"]): m for m in digest["beta_memories"]}
    assert beta[("r", 0)]["tokens"] == 1
    assert beta[("r", 1)]["tokens"] == 0
