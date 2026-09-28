"""Offline end-to-end demonstration.

Run from the repository root::

    python3 examples/demo.py

Shows, with a stable run id and engine version:
  1. rule compilation and shared alpha memories
  2. multi-fact joins + agenda ordering with per-activation sources
  3. bounded firing with chaining and stop
  4. retract cascade deleting dependent matches
  5. duplicate fact semantics
  6. the independent brute-force oracle agreeing at each checkpoint
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reteapp.core.engine import Engine
from reteapp.lang import compile_rules, parse_rules_json
from tests.reference_matcher import diff_conflict_sets, full_conflict_set

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def heading(text: str) -> None:
    print("\n" + "=" * 72)
    print(text)
    print("=" * 72)


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def check_oracle(engine, rules, label: str) -> None:
    wmes = [
        {"wme_id": w["wme_id"], "type": w["type"], "fields": w["fields"]}
        for w in engine.working_memory()
    ]
    expected = full_conflict_set(rules, wmes)
    delta = diff_conflict_sets(expected, engine.conflict_set())
    verdict = "AGREE" if not delta["missing"] and not delta["extra"] else "MISMATCH"
    print(f"[oracle:{label}] {verdict}  expected={len(expected)} actual={len(engine.conflict_set())}")
    if verdict != "AGREE":
        print("  missing:", delta["missing"])
        print("  extra  :", delta["extra"])
        raise SystemExit(1)


def main() -> None:
    from reteapp.version import __version__

    print(f"Rete engine version: {__version__}")

    heading("1. Shared-condition rules + multi-fact joins")
    doc = load("rules_orders.json")
    rules = parse_rules_json(doc)
    engine = Engine(compile_rules(rules), max_fire_rounds=50)
    for fact in [
        {"type": "Customer", "fields": {"id": "c1", "tier": "gold", "city": "NYC"}},
        {"type": "Customer", "fields": {"id": "c2", "tier": "silver", "city": "NYC"}},
        {"type": "Customer", "fields": {"id": "c3", "tier": "gold", "city": "LA"}},
        {"type": "Order", "fields": {"customer": "c1", "amount": 250}},
        {"type": "Order", "fields": {"customer": "c2", "amount": 40}},
        {"type": "Order", "fields": {"customer": "c3", "amount": 500}},
    ]:
        engine.insert_fact(fact)
    check_oracle(engine, rules, "after-inserts")
    for act in engine.activations():
        print(
            f"  salience={act['salience']:>3} seq={act['sequence']:>2} "
            f"{act['stable_key']:<40} bindings={act['bindings']}"
        )
        for src in act["sources"]:
            print(f"        source CE#{src['ce_index']}: wme {src['wme_id']} "
                  f"{src['type']} {src['fields']}")

    heading("2. Bounded firing (salience order), actions assert new facts")
    report = engine.fire_all()
    print("status:", report.status, "rounds:", report.rounds_used)
    for fired in report.fired:
        print(f"  fired {fired.stable_key} -> asserted wmes {fired.asserted}")

    heading("3. Retract cascade: remove Order wme 4")
    engine.retract_fact(4)
    check_oracle(engine, rules, "after-retract-4")
    print("remaining agenda:", [a["stable_key"] for a in engine.activations()])

    heading("4. Rule chaining with bounded loop guard")
    chain_doc = load("rules_chain.json")
    chain_rules = parse_rules_json(chain_doc)
    chained = Engine(compile_rules(chain_rules), max_fire_rounds=10)
    chained.insert_fact({"type": "Seed", "fields": {"n": 7}})
    chain_report = chained.fire_all()
    print("chain status:", chain_report.status)
    for fired in chain_report.fired:
        print(f"  {fired.rule} asserted={fired.asserted} retracted={fired.retracted}")
    check_oracle(chained, chain_rules, "after-chain")

    loop_doc = load("rules_loop.json")
    looping = Engine(compile_rules(parse_rules_json(loop_doc)), max_fire_rounds=5)
    looping.insert_fact({"type": "Counter", "fields": {"n": 0}})
    loop_report = looping.fire_all()
    print(f"self-triggering rule: status={loop_report.status} "
          f"rounds_used={loop_report.rounds_used} remaining={loop_report.remaining_activations}")

    heading("5. Duplicate fact semantics")
    dup = Engine(compile_rules(rules), max_fire_rounds=10)
    fact = {"type": "Customer", "fields": {"id": "c1", "tier": "gold", "city": "NYC"}}
    a = dup.insert_fact(fact)
    b = dup.insert_fact(fact)
    print(f"multiset: two inserts -> distinct wmes {a.wme_id}, {b.wme_id}; "
          f"second marked duplicate={b.duplicate}")
    dup.retract_fact(a.wme_id)
    print(f"after retracting first twin, second survives: "
          f"{[w['wme_id'] for w in dup.working_memory()]}")

    print("\nDEMO OK - all oracle checkpoints agreed.")


if __name__ == "__main__":
    main()
