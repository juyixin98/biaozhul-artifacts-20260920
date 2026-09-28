"""Rule generation behavior: pruning, causation boundaries, warnings, rejection."""
from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, List

import pytest

from app.models import RuleQuery, RuleStatus, WarningCode
from app.service import RuleAuditService
from app.validation import InvalidReason, QueryValidationError, validate_rule_request
from tests.reference import exact_metrics, normalized_transactions


def _rule_map(result_rules: Any) -> Dict[tuple, Any]:
    return {(r.antecedent, r.consequent): r for r in result_rules}


def test_generated_rules_match_hand_computed_metrics(
    ingested: tuple[RuleAuditService, int],
    raw_transactions: List[List[str]],
    expected_metrics_data: Dict[str, Any],
) -> None:
    service, dataset_id = ingested
    result = service.audit_rules(dataset_id, RuleQuery(min_confidence=0.0))
    by_rule = _rule_map(result.rules)
    txns = normalized_transactions(raw_transactions)

    # Rules whose union never occurs (mutually exclusive) are absent from the
    # frequent-itemset table by construction and are verified at kernel level;
    # every other hand-computed rule must be present.
    for rule_name, expected in expected_metrics_data["rules"].items():
        ant_str, cons_str = rule_name.split("=>")
        a = tuple(x for x in ant_str.split(",") if x)
        c = tuple(x for x in cons_str.split(",") if x)
        if expected["union"] == 0:
            assert (a, c) not in by_rule, f"{rule_name} must not derive from a zero-support itemset"
            continue
        rule = by_rule[(a, c)]
        m = rule.metrics
        assert Fraction(m.support).limit_denominator() == Fraction(*expected["support"]), rule_name
        assert Fraction(m.confidence).limit_denominator() == Fraction(*expected["confidence"]), rule_name
        assert Fraction(m.lift).limit_denominator() == Fraction(*expected["lift"]), rule_name
        assert Fraction(m.leverage).limit_denominator() == Fraction(*expected["leverage"]), rule_name

        oracle = exact_metrics(txns, a, c)
        assert Fraction(m.confidence).limit_denominator() == oracle["confidence"]
        assert Fraction(m.lift).limit_denominator() == oracle["lift"]


def test_min_confidence_pruning_keeps_every_rule_with_exact_status(
    ingested: tuple[RuleAuditService, int],
    raw_transactions: List[List[str]],
) -> None:
    service, dataset_id = ingested
    threshold = 0.8
    result = service.audit_rules(
        dataset_id, RuleQuery(min_confidence=threshold)
    )
    # No rule silently disappears: rejected rules stay in the audit output.
    txns = normalized_transactions(raw_transactions)
    assert len(result.rules) > 0

    for rule in result.rules:
        oracle = exact_metrics(txns, rule.antecedent, rule.consequent)
        expected_pass = oracle["confidence"] >= Fraction(threshold).limit_denominator()
        if expected_pass:
            assert rule.status in {RuleStatus.ACCEPTED, RuleStatus.INDETERMINATE}
        else:
            assert rule.status == RuleStatus.REJECTED
            assert any("min_confidence" in reason for reason in rule.reasons)

    by_rule = _rule_map(result.rules)
    # Boundary inclusion: u -> a confidence is exactly 4/5 = 0.8 -> not pruned.
    assert by_rule[(("u",), ("a",))].status in {RuleStatus.ACCEPTED, RuleStatus.INDETERMINATE}
    # Just below: a -> b confidence 3/4 = 0.75 -> pruned with explicit reason.
    ab = by_rule[(("a",), ("b",))]
    assert ab.status == RuleStatus.REJECTED
    assert Fraction(ab.metrics.confidence).limit_denominator() == Fraction(3, 4)


def test_min_lift_filter_does_not_mistake_confidence_for_association(
    ingested: tuple[RuleAuditService, int],
) -> None:
    service, dataset_id = ingested
    # a -> u has confidence 1.0 but lift exactly 1 (independence).
    result = service.audit_rules(dataset_id, RuleQuery(min_confidence=0.0, min_lift=1.5))
    by_rule = _rule_map(result.rules)
    rule = by_rule[(("a",), ("u",))]
    assert rule.metrics.confidence == pytest.approx(1.0)
    assert rule.metrics.lift == pytest.approx(1.0)
    assert rule.status == RuleStatus.REJECTED
    assert any("min_lift" in reason for reason in rule.reasons)


def test_high_confidence_independent_rule_carries_causation_warning(
    ingested: tuple[RuleAuditService, int],
) -> None:
    service, dataset_id = ingested
    result = service.audit_rules(dataset_id, RuleQuery())
    by_rule = _rule_map(result.rules)
    rule = by_rule[(("a",), ("u",))]
    assert rule.status in {RuleStatus.ACCEPTED, RuleStatus.INDETERMINATE}
    assert WarningCode.LIFT_NOT_CAUSATION.value in rule.warnings


def test_sample_size_and_rare_event_warnings_are_attached(
    ingested: tuple[RuleAuditService, int],
) -> None:
    service, dataset_id = ingested
    result = service.audit_rules(dataset_id, RuleQuery())
    by_rule = _rule_map(result.rules)

    rare = by_rule[(("a",), ("e",))]  # co-occurs once: union=1
    assert rare.metrics.support_count == 1
    assert WarningCode.RARE_RULE.value in rare.warnings
    assert WarningCode.RARE_CONSEQUENT.value in rare.warnings  # |e|=2 < 5
    assert WarningCode.SMALL_SAMPLE.value in rare.warnings     # n=5 < 30
    # Defined and numerically passing, but evidence too weak -> indeterminate.
    assert rare.status == RuleStatus.INDETERMINATE

    prevalent = by_rule[(("b",), ("a",))]
    assert WarningCode.SMALL_SAMPLE.value in prevalent.warnings


def test_positive_association_rule_is_accepted(
    ingested: tuple[RuleAuditService, int],
) -> None:
    service, dataset_id = ingested
    # A larger synthetic corpus where the target rule has adequate counts.
    rows = []
    rows += [["x", "y"]] * 40
    rows += [["x"]] * 10
    rows += [["y"]] * 10
    rows += [["z"]] * 40
    result = service.ingest_dataset("sized", rows, min_support=0.1, overwrite=True)
    out = service.audit_rules(
        result.dataset_id, RuleQuery(min_confidence=0.8, min_lift=1.1)
    )
    by_rule = _rule_map(out.rules)
    rule = by_rule[(("x",), ("y",))]
    assert rule.status == RuleStatus.ACCEPTED
    assert rule.warnings == ()
    # conf = 40/50 = 0.8; lift = 100*40/(50*50) = 1.6; lev = 0.4 - 0.25 = 0.15
    assert rule.metrics.confidence == pytest.approx(0.8)
    assert rule.metrics.lift == pytest.approx(1.6)
    assert rule.metrics.leverage == pytest.approx(0.15)


def test_leverage_filter_rejects_negative_and_zero(
    ingested: tuple[RuleAuditService, int],
) -> None:
    service, dataset_id = ingested
    result = service.audit_rules(dataset_id, RuleQuery(min_leverage=0.01))
    by_rule = _rule_map(result.rules)
    assert by_rule[(("a",), ("u",))].status == RuleStatus.REJECTED  # leverage 0
    assert by_rule[(("a",), ("e",))].status == RuleStatus.REJECTED  # leverage -0.12
    assert by_rule[(("b",), ("a",))].status in {RuleStatus.ACCEPTED, RuleStatus.INDETERMINATE}


def test_empty_antecedent_and_consequent_are_rejected_by_category() -> None:
    _query, _support, issues = validate_rule_request(antecedent=["  "])
    assert any(i.reason == InvalidReason.EMPTY_ANTECEDENT for i in issues)

    _query, _support, issues = validate_rule_request(consequent=[])
    assert any(i.reason == InvalidReason.EMPTY_CONSEQUENT for i in issues)


def test_overlapping_and_out_of_range_inputs_are_rejected_by_category() -> None:
    _, _, issues = validate_rule_request(antecedent=["a"], consequent=["a", "b"])
    assert any(i.reason == InvalidReason.OVERLAPPING_SIDES for i in issues)

    _, _, issues = validate_rule_request(min_confidence=1.5)
    assert any(i.reason == InvalidReason.THRESHOLD_OUT_OF_RANGE for i in issues)

    _, _, issues = validate_rule_request(min_confidence="high")
    assert any(i.reason == InvalidReason.THRESHOLD_WRONG_TYPE for i in issues)

    _, _, issues = validate_rule_request(max_rules=0)
    assert any(i.reason == InvalidReason.MAX_RULES_NON_POSITIVE for i in issues)

    _, _, issues = validate_rule_request(min_support=0.0)
    assert any(i.reason == InvalidReason.THRESHOLD_OUT_OF_RANGE for i in issues)


def test_service_raises_typed_validation_error(
    ingested: tuple[RuleAuditService, int],
) -> None:
    service, dataset_id = ingested
    with pytest.raises(QueryValidationError) as exc:
        service.audit_rules_raw(dataset_id, antecedent=[])
    assert exc.value.issues[0].reason == InvalidReason.EMPTY_ANTECEDENT


def test_side_filters_restrict_generation(
    ingested: tuple[RuleAuditService, int],
) -> None:
    service, dataset_id = ingested
    result = service.audit_rules(
        dataset_id, RuleQuery(antecedent=("a",), consequent=("u",))
    )
    assert len(result.rules) == 1
    assert result.rules[0].antecedent == ("a",)
    assert result.rules[0].consequent == ("u",)


def test_zero_support_side_yields_undefined_status_with_explicit_reason() -> None:
    # Synthetic externally supplied table (downward closure intentionally
    # violated) exercises the undefined path through the public generator:
    # {a,b} observed once while singleton a is absent (count 0).
    from app.indices import FrequentItemsetTable
    from app.mining import generate_rules

    table = FrequentItemsetTable.from_counts(
        {("a", "b"): 1, ("b",): 3, ("a",): 0}, n_transactions=5
    )

    def resolver(items: Any) -> int:
        return {("a",): 0, ("b",): 3, ("a", "b"): 1}[tuple(sorted(items))]

    rules = generate_rules(table, RuleQuery(), count_resolver=resolver)
    a_to_b = next(r for r in rules if r.antecedent == ("a",))
    assert a_to_b.status == RuleStatus.UNDEFINED
    assert a_to_b.metrics.confidence is None
    assert a_to_b.metrics.lift is None
    assert "antecedent support count is 0" in a_to_b.reasons[0]


def test_incomplete_table_without_resolver_raises_keyerror() -> None:
    from app.indices import FrequentItemsetTable
    from app.mining import generate_rules

    # Table has the pair {a,b} but is missing the singleton subsets entirely.
    table = FrequentItemsetTable.from_counts({("a", "b"): 2}, n_transactions=5)
    with pytest.raises(KeyError, match="missing a subset"):
        generate_rules(table, RuleQuery())

    def resolver(items: Any) -> int:
        counts = {("a",): 4, ("b",): 3, ("a", "b"): 2}
        return counts[tuple(sorted(items))]

    rules = generate_rules(table, RuleQuery(), count_resolver=resolver)
    assert len(rules) == 2  # a->b and b->a
    rule_ab = next(r for r in rules if r.antecedent == ("a",))
    assert Fraction(rule_ab.metrics.confidence).limit_denominator() == Fraction(1, 2)

