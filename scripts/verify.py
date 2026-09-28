#!/usr/bin/env python3
"""End-to-end verification of the lift-audit pipeline.

Runs the real pipeline (corpus ingest -> mining -> rule generation ->
evaluation) against the fixture corpora and asserts hand-computed
reference values. Prints one line per check and exits non-zero on any
failure. Checks that cannot be executed in this environment are listed
explicitly under NOT EXECUTED — never reported as passed.

Usage:  python scripts/verify.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app.index.db import Store
from app.corpus.loader import load_fixture
from app.service import AuditService
from app.mining.rules import (
    W_HIGH_CONF_LOW_LIFT,
    W_RARE_EVENT,
    W_SMALL_SAMPLE,
    W_UBIQUITOUS_CONSEQUENT,
)
from tests.hand_computed import (
    ALPHA_TO_BETA,
    APPLE_TO_BASE,
    BASIC_FREQUENT_0_6,
    BEER_TO_DIAPERS,
    MILK_DIAPERS_TO_BEER,
    RAREA_TO_RAREB,
)

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
TOL = 1e-9

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))


def close(a: float | None, b: float) -> bool:
    return a is not None and abs(a - b) < TOL


def metrics_match(rule, expected) -> bool:
    return (
        close(rule.metrics.support, float(expected["support"]))
        and close(rule.metrics.confidence, float(expected["confidence"]))
        and close(rule.metrics.lift, float(expected["lift"]))
        and close(rule.metrics.leverage, float(expected["leverage"]))
    )


def find_rule(rules, ante, cons):
    for r in rules:
        if r.antecedent == frozenset(ante) and r.consequent == frozenset(cons):
            return r
    return None


def main() -> int:
    service = AuditService(Store(":memory:"), Settings(db_path=":memory:"))

    def load(name: str) -> int:
        corpus_name, txs = load_fixture(FIXTURES / f"{name}.json")
        return service.create_corpus(corpus_name, txs).corpus_id

    # 1. Itemset mining on the basic table matches hand-computed sets.
    basic = load("basic")
    itemsets = service.mine(basic, 0.6)
    got = {frozenset(i["items"]): i["support_count"] for i in itemsets}
    check("basic: frequent itemsets @ min_support=0.6", got == BASIC_FREQUENT_0_6,
          f"got {got}")

    # 2. Hand-computed rule metrics.
    rules = service.generate_rules(basic, min_confidence=0.7, min_lift=None)
    r = find_rule(rules, ["beer"], ["diapers"])
    check("basic: {beer}->{diapers} conf=1 lift=1.25 leverage=0.12",
          r is not None and metrics_match(r, BEER_TO_DIAPERS))

    service.mine(basic, 0.4)
    rules = service.generate_rules(basic, min_confidence=0.6, min_lift=None)
    r = find_rule(rules, ["milk", "diapers"], ["beer"])
    check("basic: {milk,diapers}->{beer} conf=2/3 lift=10/9 leverage=0.04",
          r is not None and metrics_match(r, MILK_DIAPERS_TO_BEER))

    # 3. Min-confidence pruning keeps exactly the expected rule set.
    service.mine(basic, 0.6)
    rules = service.generate_rules(basic, min_confidence=0.8, min_lift=None)
    check("basic: min_confidence=0.8 keeps exactly {beer}->{diapers}",
          len(rules) == 1
          and rules[0].antecedent == frozenset(["beer"])
          and rules[0].consequent == frozenset(["diapers"]),
          f"got {[(sorted(x.antecedent), sorted(x.consequent)) for x in rules]}")

    # 4. Ubiquitous consequent: confidence 1.0 is flagged, not causal.
    ub = load("ubiquitous")
    service.mine(ub, 0.5)
    rules = service.generate_rules(ub, min_confidence=0.9, min_lift=None)
    r = find_rule(rules, ["apple"], ["base"])
    check("ubiquitous: {apple}->{base} conf=1 lift=1 leverage=0",
          r is not None and metrics_match(r, APPLE_TO_BASE))
    check("ubiquitous: HIGH_CONFIDENCE_LOW_LIFT + UBIQUITOUS_CONSEQUENT warnings",
          r is not None and W_HIGH_CONF_LOW_LIFT in r.warnings
          and W_UBIQUITOUS_CONSEQUENT in r.warnings)

    # 5. Mutually exclusive items: negative leverage, zero lift.
    ex = load("exclusive")
    r = service.evaluate(ex, ["alpha"], ["beta"])
    check("exclusive: {alpha}->{beta} conf=0 lift=0 leverage=-0.25",
          metrics_match(r, ALPHA_TO_BETA))

    # 6. Rare combination: perfect confidence on 2 observations is warned.
    rare = load("rare_combo")
    service.mine(rare, 0.01)
    rules = service.generate_rules(rare, min_confidence=0.5, min_lift=None)
    r = find_rule(rules, ["rareA"], ["rareB"])
    check("rare_combo: {rareA}->{rareB} conf=1 lift=20 leverage=0.0475",
          r is not None and metrics_match(r, RAREA_TO_RAREB))
    check("rare_combo: RARE_EVENT warning present, SMALL_SAMPLE absent",
          r is not None and W_RARE_EVENT in r.warnings
          and W_SMALL_SAMPLE not in r.warnings)

    # 7. Zero denominator is undefined (None), never 0 or NaN.
    r = service.evaluate(basic, ["caviar"], ["beer"])
    check("zero denominator: confidence/lift undefined for unseen antecedent",
          r.metrics.confidence is None and r.metrics.lift is None
          and r.metrics.support == 0.0)

    # 8. Scope rejections carry stable categories.
    from app.validation.queries import QueryRejected
    try:
        service.evaluate(basic, [], ["beer"])
        check("empty antecedent rejected", False, "no exception raised")
    except QueryRejected as exc:
        check("empty antecedent rejected", exc.category == "EMPTY_ANTECEDENT")
    try:
        service.evaluate(basic, ["beer"], [])
        check("empty consequent rejected", False, "no exception raised")
    except QueryRejected as exc:
        check("empty consequent rejected", exc.category == "EMPTY_CONSEQUENT")

    # 9. Small-sample warning on the 5-transaction corpus.
    rules = service.generate_rules(basic, min_confidence=0.7, min_lift=None)
    check("basic: SMALL_SAMPLE warning on every rule (N=5 < 30)",
          bool(rules) and all(W_SMALL_SAMPLE in x.warnings for x in rules))

    # -- checks that cannot be executed here, listed explicitly ----------
    not_executed = [
        "large-corpus performance check (no production-scale data in this environment)",
        "concurrent-write behavior of the SQLite store (single-process scope only)",
    ]
    for item in not_executed:
        print(f"NOT EXECUTED  {item}")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed, "
          f"{len(not_executed)} not executed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
