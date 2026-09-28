"""Query validation: every rejection at the boundary is explicit and typed."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Sequence, Tuple

from .models import (
    MAX_MIN_CONFIDENCE,
    MAX_MIN_SUPPORT,
    MIN_MIN_CONFIDENCE,
    MIN_MIN_SUPPORT,
    Itemset,
    RuleQuery,
    normalize_items,
)


class InvalidReason(str, Enum):
    EMPTY_ANTECEDENT = "empty_antecedent"
    EMPTY_CONSEQUENT = "empty_consequent"
    OVERLAPPING_SIDES = "overlapping_antecedent_consequent"
    THRESHOLD_OUT_OF_RANGE = "threshold_out_of_range"
    THRESHOLD_WRONG_TYPE = "threshold_wrong_type"
    MAX_RULES_NON_POSITIVE = "max_rules_non_positive"
    ITEM_WRONG_TYPE = "item_wrong_type"


@dataclass(frozen=True)
class ValidationIssue:
    reason: InvalidReason
    field: str
    detail: str


class QueryValidationError(ValueError):
    def __init__(self, issues: Sequence[ValidationIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{i.field}: {i.reason.value} ({i.detail})" for i in issues))


def _validate_threshold(
    name: str, value: Any, low: float, high: float
) -> List[ValidationIssue]:
    if value is None:
        return []
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return [
            ValidationIssue(
                InvalidReason.THRESHOLD_WRONG_TYPE,
                name,
                f"expected number, got {type(value).__name__}",
            )
        ]
    if not (low <= float(value) <= high):
        return [
            ValidationIssue(
                InvalidReason.THRESHOLD_OUT_OF_RANGE,
                name,
                f"{value} not in [{low}, {high}]",
            )
        ]
    return []


def _validate_side(
    raw_items: Any, side: str, empty_reason: InvalidReason
) -> Tuple[Itemset | None, List[ValidationIssue]]:
    if raw_items is None:
        return None, []
    issues: List[ValidationIssue] = []
    if not isinstance(raw_items, (list, tuple)):
        return None, [
            ValidationIssue(
                InvalidReason.ITEM_WRONG_TYPE,
                side,
                f"expected list of item strings, got {type(raw_items).__name__}",
            )
        ]
    for item in raw_items:
        if not isinstance(item, str):
            issues.append(
                ValidationIssue(
                    InvalidReason.ITEM_WRONG_TYPE,
                    side,
                    f"items must be strings, got {type(item).__name__}",
                )
            )
    if issues:
        return None, issues

    items = normalize_items(raw_items)
    if not items:
        return (), [
            ValidationIssue(
                empty_reason,
                side,
                f"{side} must contain at least one non-blank item; "
                "empty antecedent/consequent are outside the rule domain",
            )
        ]
    return items, []


def validate_rule_request(
    *,
    min_confidence: Any = 0.0,
    min_lift: Any = None,
    min_leverage: Any = None,
    min_support: Any = None,
    max_rules: Any = None,
    antecedent: Any = None,
    consequent: Any = None,
) -> Tuple[RuleQuery, float | None, List[ValidationIssue]]:
    """Validate raw request values. Returns (query, min_support, issues).

    On any issue the caller must reject the request; the returned query is
    still populated for diagnostics but must not be executed.
    """
    issues: List[ValidationIssue] = []

    issues += _validate_threshold(
        "min_confidence", min_confidence, MIN_MIN_CONFIDENCE, MAX_MIN_CONFIDENCE
    )
    issues += _validate_threshold("min_lift", min_lift, 0.0, float("inf"))
    issues += _validate_threshold("min_leverage", min_leverage, -1.0, 1.0)
    min_support_bad = False
    if min_support is not None:
        support_issues = _validate_threshold(
            "min_support", min_support, MIN_MIN_SUPPORT, MAX_MIN_SUPPORT
        )
        if not support_issues and float(min_support) <= 0.0:
            support_issues = [
                ValidationIssue(
                    InvalidReason.THRESHOLD_OUT_OF_RANGE,
                    "min_support",
                    "min_support must be > 0: zero-support itemsets are not "
                    "frequent and cannot be enumerated from observed transactions",
                )
            ]
        issues += support_issues
        min_support_bad = bool(support_issues)
    validated_min_support = (
        None if min_support is None or min_support_bad else float(min_support)
    )

    if max_rules is not None:
        if isinstance(max_rules, bool) or not isinstance(max_rules, int) or max_rules <= 0:
            issues.append(
                ValidationIssue(
                    InvalidReason.MAX_RULES_NON_POSITIVE,
                    "max_rules",
                    f"max_rules must be a positive integer, got {max_rules!r}",
                )
            )

    ant, ant_issues = _validate_side(
        antecedent, "antecedent", InvalidReason.EMPTY_ANTECEDENT
    )
    cons, cons_issues = _validate_side(
        consequent, "consequent", InvalidReason.EMPTY_CONSEQUENT
    )
    issues += ant_issues + cons_issues

    if ant and cons and set(ant) & set(cons):
        overlap = sorted(set(ant) & set(cons))
        issues.append(
            ValidationIssue(
                InvalidReason.OVERLAPPING_SIDES,
                "antecedent/consequent",
                # Deliberately does not echo item names: they may be sensitive.
                f"{len(overlap)} item(s) appear on both sides of the rule; "
                "antecedent and consequent must be disjoint",
            )
        )

    query = RuleQuery(
        min_confidence=float(min_confidence) if isinstance(min_confidence, (int, float)) and not isinstance(min_confidence, bool) else 0.0,
        min_lift=float(min_lift) if isinstance(min_lift, (int, float)) and not isinstance(min_lift, bool) else None,
        min_leverage=float(min_leverage) if isinstance(min_leverage, (int, float)) and not isinstance(min_leverage, bool) else None,
        max_rules=max_rules if isinstance(max_rules, int) and not isinstance(max_rules, bool) and max_rules > 0 else None,
        antecedent=ant if ant else None,
        consequent=cons if cons else None,
    )
    return query, validated_min_support, issues
