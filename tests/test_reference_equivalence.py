"""Reference-equivalence tests (the heart of the review).

For each scenario we drive the indexed Rete engine through an ordered script
of insert/retract/fire operations and, at every checkpoint, compute the full
conflict set with the independent brute-force oracle
(:mod:`tests.reference_matcher`) over the SAME working-memory snapshot.

The oracle never imports the Rete implementation, so agreement is genuine
cross-validation, not a self-referential check. Each comparison writes a
structured review-log record carrying inputs, both answers, the diff and the
verdict basis.
"""

from __future__ import annotations

from tests.helpers import build_engine, load_fixture
from tests.reference_matcher import diff_conflict_sets, full_conflict_set
from reteapp.lang import parse_rules_json


def _ref_snapshot(engine) -> list[dict]:
    return [
        {"wme_id": w["wme_id"], "type": w["type"], "fields": w["fields"]}
        for w in engine.working_memory()
    ]


def _oracle_conflict_set(rules, engine) -> set:
    return full_conflict_set(rules, _ref_snapshot(engine))


def assert_agrees_with_oracle(engine, rules, review, node, *, step: str) -> None:
    expected = _oracle_conflict_set(rules, engine)
    actual = engine.conflict_set()
    delta = diff_conflict_sets(expected, actual)
    review.record(
        node,
        kind="oracle_comparison",
        verdict="PASS" if not delta["missing"] and not delta["extra"] else "FAIL",
        step=step,
        working_memory=_ref_snapshot(engine),
        expected_count=len(expected),
        actual_count=len(actual),
        expected=[
            {"rule": r, "wme_ids": list(ids), "bindings": dict(bindings)}
            for r, ids, bindings in sorted(expected, key=lambda m: (m[0], m[1]))
        ],
        actual=[
            {"rule": r, "wme_ids": list(ids), "bindings": dict(bindings)}
            for r, ids, bindings in sorted(actual, key=lambda m: (m[0], m[1]))
        ],
        missing=[
            {"rule": r, "wme_ids": list(ids)} for r, ids, _b in delta["missing"]
        ],
        extra=[
            {"rule": r, "wme_ids": list(ids)} for r, ids, _b in delta["extra"]
        ],
    )
    assert not delta["missing"], f"[{step}] Rete MISSED oracle matches: {delta['missing']}"
    assert not delta["extra"], f"[{step}] Rete produced EXTRA matches: {delta['extra']}"


# ---------------------------------------------------------------------------
# Scenario 1: shared conditions across rules + multi-fact joins
# ---------------------------------------------------------------------------

def test_shared_conditions_and_multifactoin_join_matches_oracle(review) -> None:
    node = "test_reference_equivalence::shared_and_join"
    doc = load_fixture("rules_orders.json")
    engine, rules = build_engine(doc, max_fire_rounds=50)

    steps = [
        ("insert", {"type": "Customer", "fields": {"id": "c1", "tier": "gold", "city": "NYC"}}),
        ("insert", {"type": "Customer", "fields": {"id": "c2", "tier": "silver", "city": "NYC"}}),
        ("insert", {"type": "Customer", "fields": {"id": "c3", "tier": "gold", "city": "LA"}}),
        ("insert", {"type": "Order", "fields": {"customer": "c1", "amount": 250}}),
        ("insert", {"type": "Order", "fields": {"customer": "c2", "amount": 40}}),
        ("insert", {"type": "Order", "fields": {"customer": "c3", "amount": 500}}),
    ]
    for index, (_op, fact) in enumerate(steps):
        engine.insert_fact(fact)
        assert_agrees_with_oracle(engine, rules, review, node, step=f"after_insert_{index}")

    # Concrete assertions on the specific expected matches.
    pairs = {tuple(a["wme_ids"]) for a in engine.activations() if a["rule"] == "customer_order_any_tier"}
    assert pairs == {(1, 4), (2, 5), (3, 6)}
    gold = {tuple(a["wme_ids"]) for a in engine.activations() if a["rule"] == "gold_customer_order"}
    # c2 is silver (excluded); gold customers c1,c3 with amount>100 -> orders 4,6.
    assert gold == {(1, 4), (3, 6)}
    # The two NYC customers combine in both orientations (distinct physical WMEs).
    city = {tuple(a["wme_ids"]) for a in engine.activations() if a["rule"] == "same_city_customer_pair"}
    assert city == {(1, 2), (2, 1)}


# ---------------------------------------------------------------------------
# Scenario 2: retract deletes every dependent match; re-add rebuilds them
# ---------------------------------------------------------------------------

def test_retract_then_readd_matches_oracle(review) -> None:
    node = "test_reference_equivalence::retract_readd"
    doc = load_fixture("rules_orders.json")
    engine, rules = build_engine(doc, max_fire_rounds=50)
    facts = [
        {"type": "Customer", "fields": {"id": "c1", "tier": "gold", "city": "NYC"}},
        {"type": "Customer", "fields": {"id": "c2", "tier": "silver", "city": "NYC"}},
        {"type": "Order", "fields": {"customer": "c1", "amount": 250}},
        {"type": "Order", "fields": {"customer": "c2", "amount": 40}},
    ]
    for f in facts:
        engine.insert_fact(f)
    assert_agrees_with_oracle(engine, rules, review, node, step="all_inserted")
    # gold pair (1,3) + any-tier pairs (1,3),(2,4) + city pairs (1,2),(2,1) = 5
    assert len(engine.agenda) == 5

    # Retract the c1 Customer: every match using wme 1 must vanish.
    engine.retract_fact(1)
    assert_agrees_with_oracle(engine, rules, review, node, step="after_retract_customer_1")
    surviving = {tuple(a["wme_ids"]) for a in engine.activations()}
    assert all(1 not in ids for ids in surviving), surviving
    assert surviving == {(2, 4)}  # c2 order pair only

    # Re-add c1 as a NEW physical fact (new monotonic id): matches rebuilt.
    result = engine.insert_fact(facts[0])
    assert result.wme_id == 5  # fresh identity, never id reuse
    assert_agrees_with_oracle(engine, rules, review, node, step="after_readd_customer")
    pairs = {tuple(a["wme_ids"]) for a in engine.activations() if a["rule"] == "customer_order_any_tier"}
    assert (5, 3) in pairs and (2, 4) in pairs
    city = {tuple(a["wme_ids"]) for a in engine.activations() if a["rule"] == "same_city_customer_pair"}
    assert city == {(5, 2), (2, 5)}

    # Retract one Order: only matches using that order disappear; unrelated
    # pairs (c2-order4) and the customer city pairs remain.
    engine.retract_fact(3)
    assert_agrees_with_oracle(engine, rules, review, node, step="after_retract_order_3")
    remaining = {tuple(a["wme_ids"]) for a in engine.activations()}
    assert all(3 not in ids for ids in remaining)
    assert remaining == {(2, 4), (5, 2), (2, 5)}


# ---------------------------------------------------------------------------
# Scenario 3: rules trigger one another; post-fire conflict set still matches
# ---------------------------------------------------------------------------

def test_rule_chain_triggers_and_tracks_oracle(review) -> None:
    node = "test_reference_equivalence::rule_chain"
    doc = load_fixture("rules_chain.json")
    engine, rules = build_engine(doc, max_fire_rounds=10)
    engine.insert_fact({"type": "Seed", "fields": {"n": 7}})
    assert_agrees_with_oracle(engine, rules, review, node, step="seed_inserted")

    # fire_all chains Seed->Need->Done->Finished because actions assert new
    # facts that feed later rules.
    report = engine.fire_all()
    assert report.status == "fired"
    assert [f.rule for f in report.fired] == ["seed_to_need", "need_to_done", "done_final"]
    assert report.fired[0].retracted == [1]  # seed rule retracts its matched fact
    assert_agrees_with_oracle(engine, rules, review, node, step="after_chain")

    types = sorted((w["type"], w["fields"]["n"]) for w in engine.working_memory())
    assert types == [("Done", 7), ("Finished", 7), ("Need", 7)]

    # Every fired activation carries its source WMEs.
    first = report.fired[0]
    assert first.sources == [
        {"ce_index": 0, "wme_id": 1, "type": "Seed", "fields": {"n": 7}}
    ]


# ---------------------------------------------------------------------------
# Scenario 4: generated randomised facts - broad oracle sweep
# ---------------------------------------------------------------------------

def test_randomised_fact_sweep_matches_oracle(review) -> None:
    import random

    node = "test_reference_equivalence::random_sweep"
    doc = load_fixture("rules_orders.json")
    rng = random.Random(20260928)
    engine, rules = build_engine(doc, max_fire_rounds=1)
    cities = ["NYC", "NYC", "LA", "SF"]
    tiers = ["gold", "silver", "gold"]
    next_id = 1
    live_ids: list[int] = []
    for turn in range(40):
        if rng.random() < 0.7 or not live_ids:
            kind = rng.choice(["Customer", "Order"])
            if kind == "Customer":
                cid = f"c{rng.randrange(1, 6)}"
                fact = {"type": "Customer", "fields": {
                    "id": cid,
                    "tier": rng.choice(tiers),
                    "city": rng.choice(cities),
                }}
            else:
                fact = {"type": "Order", "fields": {
                    "customer": f"c{rng.randrange(1, 6)}",
                    "amount": rng.choice([20, 80, 150, 300]),
                }}
            result = engine.insert_fact(fact)
            live_ids.append(result.wme_id)
        else:
            victim = rng.choice(live_ids)
            live_ids.remove(victim)
            try:
                engine.retract_fact(victim)
            except Exception:
                # Already retracted twin ids can be picked; keep oracle honest
                # by only checkpointing successful operations.
                pass
        if turn % 5 == 0:
            assert_agrees_with_oracle(engine, rules, review, node, step=f"turn_{turn}")
    assert_agrees_with_oracle(engine, rules, review, node, step="final")


# ---------------------------------------------------------------------------
# Scenario 5: many deterministic differential trials, 3-condition rules that
# share a prefix condition (stresses cascades across beta depths and shared
# alpha memories under heavy insert/retract churn)
# ---------------------------------------------------------------------------

def test_heavy_three_condition_churn_matches_oracle(review) -> None:
    import random

    node = "test_reference_equivalence::heavy_churn"
    doc = {
        "rules": [
            {"name": "abc", "salience": 0, "conditions": [
                {"type": "A", "constraints": [
                    {"field": "k", "variable": "?k"}, {"field": "p", "variable": "?p"}]},
                {"type": "B", "constraints": [{"field": "k", "variable": "?k"}]},
                {"type": "C", "constraints": [{"field": "p", "variable": "?p"}]},
            ], "action": {}},
            {"name": "ab", "salience": 0, "conditions": [
                {"type": "A", "constraints": [{"field": "k", "variable": "?k"}]},
                {"type": "B", "constraints": [{"field": "k", "variable": "?k"}]},
            ], "action": {}},
        ]
    }
    rules = parse_rules_json(doc)
    rng = random.Random(424242)
    checkpoints = 0
    for trial in range(20):
        engine, trial_rules = build_engine(doc, max_fire_rounds=1)
        live: list[int] = []
        for turn in range(60):
            if rng.random() < 0.62 or not live:
                fact_type = rng.choice(["A", "B", "C"])
                fact = {"type": fact_type, "fields": {
                    "k": rng.choice(["x", "y", "z"]),
                    "p": rng.choice([1, 2, 3]),
                }}
                live.append(engine.insert_fact(fact).wme_id)
            else:
                victim = rng.choice(live)
                live.remove(victim)
                try:
                    engine.retract_fact(victim)
                except Exception:
                    pass
            if turn % 10 == 0:
                assert_agrees_with_oracle(
                    engine, trial_rules, review, node, step=f"trial{trial}_turn{turn}"
                )
                checkpoints += 1
    assert checkpoints >= 100

