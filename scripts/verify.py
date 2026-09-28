#!/usr/bin/env python
"""Standalone verification script for the association-rule lift audit.

Runs three INDEPENDENT strands of evidence, none of which reuse the mining
kernel to produce its own expected answers:

  1. Hand-computed expected fractions loaded from
     tests/fixtures/expected_metrics.json (worked out on paper).
  2. A brute-force oracle in tests/reference.py (naive set loops over rows).
  3. mlxtend's apriori/association_rules as a third-party reference library
     (skipped with a clear note if it is not installed).

It also exercises the required boundary behaviors:
  - zero denominator -> undefined, not infinity;
  - empty antecedent/consequent -> rejected by category;
  - in-transaction duplicates collapse (cross-checked against mlxtend);
  - high confidence with lift == 1 is flagged, not treated as causal;
  - small-sample and rare-event warnings appear in results.

Exit code 0 only when every executed check passes. Checks that cannot run
(e.g. mlxtend missing) are reported as SKIPPED, never as passed.

Usage:
    .venv/bin/python scripts/verify.py
"""
from __future__ import annotations

import json
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.corpus import build_corpus  # noqa: E402
from app.indices import FrequentItemsetTable, TransactionIndex  # noqa: E402
from app.mining import AuditThresholds, compute_metrics, generate_rules  # noqa: E402
from app.models import RuleQuery, RuleStatus, WarningCode  # noqa: E402
from app.validation import InvalidReason, validate_rule_request  # noqa: E402
from tests.reference import exact_metrics, normalized_transactions  # noqa: E402

passed = 0
failed = 0
skipped: List[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
        return True
    failed += 1
    print(f"  FAIL  {name} {detail}")
    return False


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def load_fixtures() -> Tuple[List[List[str]], Dict[str, Any]]:
    fixtures = ROOT / "tests" / "fixtures"
    corpus = json.loads((fixtures / "canonical_corpus.json").read_text())
    metrics = json.loads((fixtures / "expected_metrics.json").read_text())
    return corpus["raw_transactions"], metrics


def f(value: float | None) -> Fraction | None:
    return None if value is None else Fraction(value).limit_denominator(100_000)


def verify_hand_computed(raw: List[List[str]], expected_doc: Dict[str, Any]) -> None:
    section("1. Hand-computed metrics + brute-force oracle")
    txns = normalized_transactions(raw)
    n = expected_doc["n_transactions"]
    check("normalized corpus has 5 transactions", len(txns) == n)

    for rule_name, expected in expected_doc["rules"].items():
        ant_s, cons_s = rule_name.split("=>")
        a = [x for x in ant_s.split(",") if x]
        c = [x for x in cons_s.split(",") if x]
        m = compute_metrics(n, expected["union"], expected["antecedent"], expected["consequent"])
        oracle = exact_metrics(txns, a, c)

        check(
            f"{rule_name}: confidence={expected['confidence']}",
            f(m.confidence) == Fraction(*expected["confidence"]) == oracle["confidence"],
        )
        check(
            f"{rule_name}: lift={expected['lift']}",
            f(m.lift) == Fraction(*expected["lift"]) == oracle["lift"],
        )
        check(
            f"{rule_name}: leverage={expected['leverage']}",
            f(m.leverage) == Fraction(*expected["leverage"]) == oracle["leverage"],
        )


def verify_undefined(expected_doc: Dict[str, Any]) -> None:
    section("2. Zero-denominator semantics are explicit")
    cases = expected_doc["undefined_cases"]

    z = cases["zero_antecedent"]
    m = compute_metrics(5, z["union"], z["antecedent"], z["consequent"])
    check("zero antecedent: confidence undefined", m.confidence is None)
    check("zero antecedent: lift undefined", m.lift is None)
    check("zero antecedent: defined flag False", m.defined is False)
    check("zero antecedent: leverage still defined 0", m.leverage == 0.0)

    z = cases["zero_consequent"]
    m = compute_metrics(5, z["union"], z["antecedent"], z["consequent"])
    check("zero consequent: confidence == 0", m.confidence == 0.0)
    check("zero consequent: lift undefined", m.lift is None)
    check("zero consequent: defined flag False", m.defined is False)


def verify_empty_sides_rejected() -> None:
    section("3. Empty antecedent/consequent rejected by category")
    _, _, issues = validate_rule_request(antecedent=["   "])
    check(
        "blank antecedent -> empty_antecedent",
        any(i.reason == InvalidReason.EMPTY_ANTECEDENT for i in issues),
    )
    _, _, issues = validate_rule_request(consequent=[])
    check(
        "empty consequent -> empty_consequent",
        any(i.reason == InvalidReason.EMPTY_CONSEQUENT for i in issues),
    )
    _, _, issues = validate_rule_request(antecedent=["a"], consequent=["a"])
    check(
        "overlapping sides -> overlapping_antecedent_consequent",
        any(i.reason == InvalidReason.OVERLAPPING_SIDES for i in issues),
    )


def verify_generation_and_warnings(raw: List[List[str]]) -> None:
    section("4. Rule generation: pruning, causation boundary, warnings")
    spec = build_corpus(raw)
    index = TransactionIndex.from_corpus(spec)
    # Build a complete table directly from observed counts so that even
    # zero-co-occurrence structure is auditable via the kernel elsewhere;
    # here we use the apriori path the API uses.
    from app.apriori import mine_frequent_itemsets

    table = mine_frequent_itemsets(index, 0.2)

    all_rules = generate_rules(
        table, RuleQuery(), thresholds=AuditThresholds(small_sample_n=30, rare_event_count=5),
        count_resolver=index.itemset_count,
    )
    by_rule = {(r.antecedent, r.consequent): r for r in all_rules}

    # High confidence != association: a -> u conf 1.0, lift exactly 1.
    au = by_rule[(("a",), ("u",))]
    check("a->u confidence is 1.0", au.metrics.confidence == 1.0)
    check("a->u lift is exactly 1.0", au.metrics.lift == 1.0)
    check(
        "a->u flagged lift_not_causation",
        WarningCode.LIFT_NOT_CAUSATION.value in au.warnings,
    )

    # Min-confidence pruning retains rules with rejected status + reason.
    pruned = generate_rules(
        table, RuleQuery(min_confidence=0.9), count_resolver=index.itemset_count
    )
    rejected = [r for r in pruned if r.status == RuleStatus.REJECTED]
    check("min_confidence=0.9 produces explicit rejections", len(rejected) > 0)
    check(
        "every rejection names min_confidence",
        all(any("min_confidence" in why for why in r.reasons) for r in rejected),
    )
    # No rule silently dropped: same candidate count with/without threshold.
    check("pruning never removes candidates from output", len(pruned) == len(all_rules))

    # Rare combination a -> e: warnings + indeterminate.
    ae = by_rule[(("a",), ("e",))]
    check("a->e co-occurs once", ae.metrics.support_count == 1)
    check("a->e rare_rule warning", WarningCode.RARE_RULE.value in ae.warnings)
    check("a->e small_sample warning", WarningCode.SMALL_SAMPLE.value in ae.warnings)
    check("a->e status indeterminate", ae.status == RuleStatus.INDETERMINATE)

    # Mutually exclusive c,d only reachable at kernel level (zero support):
    m = compute_metrics(5, 0, 3, 2)
    check("c,d exclusive: lift 0", m.lift == 0.0)
    check("c,d exclusive: leverage -6/25", f(m.leverage) == Fraction(-6, 25))


def verify_dedup_against_mlxtend(raw: List[List[str]]) -> None:
    section("5. mlxtend cross-validation (third-party reference)")
    try:
        import pandas as pd
        from mlxtend.frequent_patterns import apriori as mlx_apriori
        from mlxtend.frequent_patterns import association_rules
    except ImportError as exc:
        skipped.append(f"mlxtend cross-check skipped ({exc}); install requirements.txt")
        print("  SKIP  mlxtend/pandas not installed - NOT counted as passed")
        return

    from app.apriori import mine_frequent_itemsets

    spec = build_corpus(raw)
    index = TransactionIndex.from_corpus(spec)

    rows = []
    for t in spec.transactions:
        rows.append(set(t.items))
    items = sorted({i for row in rows for i in row})
    onehot = pd.DataFrame([{it: (it in row) for it in items} for row in rows])

    for min_support in (0.2, 0.4):
        ours = mine_frequent_itemsets(index, min_support)
        theirs = mlx_apriori(onehot, min_support=min_support, use_colnames=True)
        ours_map = {frozenset(fi.items): fi.support for fi in ours.all()}
        theirs_map = {
            frozenset(k): float(v)
            for k, v in zip(theirs["itemsets"], theirs["support"])
        }
        check(
            f"min_support={min_support}: identical itemset family",
            set(ours_map) == set(theirs_map),
        )
        check(
            f"min_support={min_support}: identical support values",
            all(abs(ours_map[k] - theirs_map[k]) < 1e-12 for k in ours_map),
        )

    # Dedup semantics: inflated duplicates must not change mlxtend agreement.
    inflated = [row + [row[0]] * 4 for row in raw if row]
    spec2 = build_corpus(inflated)
    idx2 = TransactionIndex.from_corpus(spec2)
    ours2 = mine_frequent_itemsets(idx2, 0.2)
    base = mine_frequent_itemsets(index, 0.2)
    check(
        "duplicated in-row items leave supports unchanged",
        {fi.items: fi.support for fi in ours2.all()}
        == {fi.items: fi.support for fi in base.all()},
    )

    # Confidence/lift cross-check against mlxtend association_rules.
    freq = mlx_apriori(onehot, min_support=0.2, use_colnames=True)
    mlx_rules = association_rules(freq, metric="confidence", min_threshold=0.0)
    ours_rules = generate_rules(base, RuleQuery(), count_resolver=index.itemset_count)
    ours_metrics = {
        (r.antecedent, r.consequent): (r.metrics.confidence, r.metrics.lift)
        for r in ours_rules
    }
    mismatches = 0
    for _, row in mlx_rules.iterrows():
        key = (tuple(sorted(row["antecedents"])), tuple(sorted(row["consequents"])))
        if key not in ours_metrics:
            mismatches += 1
            continue
        oc, ol = ours_metrics[key]
        if abs(oc - float(row["confidence"])) > 1e-12 or abs(ol - float(row["lift"])) > 1e-12:
            mismatches += 1
    check("confidence and lift agree with mlxtend for every rule", mismatches == 0,
          f"mismatches={mismatches}")


def main() -> int:
    print("Association Rule Lift Audit - verification")
    raw, expected_doc = load_fixtures()
    verify_hand_computed(raw, expected_doc)
    verify_undefined(expected_doc)
    verify_empty_sides_rejected()
    verify_generation_and_warnings(raw)
    verify_dedup_against_mlxtend(raw)

    print("\n=== Summary ===")
    print(f"passed={passed} failed={failed} skipped={len(skipped)}")
    for note in skipped:
        print(f"  SKIPPED (not executed): {note}")
    if failed:
        print("VERIFICATION FAILED")
        return 1
    if skipped:
        print("ALL EXECUTED CHECKS PASSED (some checks skipped - see above)")
    else:
        print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
