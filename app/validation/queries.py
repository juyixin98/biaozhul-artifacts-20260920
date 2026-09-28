"""Query validation: reject out-of-scope rule queries before mining.

Failure categories are stable strings so API clients and tests assert on
the class of failure, not on message text.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..diagnostics import log_decision, mask_items

# Categories
EMPTY_ANTECEDENT = "EMPTY_ANTECEDENT"
EMPTY_CONSEQUENT = "EMPTY_CONSEQUENT"
OVERLAPPING_SIDES = "OVERLAPPING_SIDES"
THRESHOLD_OUT_OF_RANGE = "THRESHOLD_OUT_OF_RANGE"
CORPUS_NOT_FOUND = "CORPUS_NOT_FOUND"
ITEMS_NOT_IN_CORPUS = "ITEMS_NOT_IN_CORPUS"


class QueryRejected(ValueError):
    """A rule query was rejected by scope validation."""

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail


@dataclass(frozen=True)
class ValidatedRuleQuery:
    antecedent: frozenset[str]
    consequent: frozenset[str]


def validate_rule_sides(
    antecedent: list[str],
    consequent: list[str],
    *,
    mask_items_in_logs: bool = True,
) -> ValidatedRuleQuery:
    """Validate the two sides of a rule query.

    Empty antecedent or consequent is rejected by scope: with an empty
    antecedent "confidence" degenerates to the consequent's prevalence,
    and an empty consequent carries no prediction — both are outside the
    scope of this audit (see docs/semantics.md).
    """
    ante = frozenset(i.strip() for i in antecedent if i and i.strip())
    cons = frozenset(i.strip() for i in consequent if i and i.strip())

    if not ante:
        log_decision(
            "rejected",
            reason=EMPTY_ANTECEDENT,
            state={"n_antecedent_items": 0},
        )
        raise QueryRejected(
            EMPTY_ANTECEDENT,
            "antecedent must contain at least one non-empty item; "
            "an empty antecedent makes confidence degenerate to prevalence",
        )
    if not cons:
        log_decision(
            "rejected",
            reason=EMPTY_CONSEQUENT,
            state={"n_consequent_items": 0},
        )
        raise QueryRejected(
            EMPTY_CONSEQUENT,
            "consequent must contain at least one non-empty item; "
            "an empty consequent carries no prediction",
        )
    overlap = ante & cons
    if overlap:
        log_decision(
            "rejected",
            reason=OVERLAPPING_SIDES,
            state={"overlap": mask_items(overlap, enabled=mask_items_in_logs)},
        )
        raise QueryRejected(
            OVERLAPPING_SIDES,
            "antecedent and consequent must be disjoint",
        )
    return ValidatedRuleQuery(antecedent=ante, consequent=cons)


def validate_threshold(name: str, value: float, *, lo: float, hi: float) -> float:
    """Range-check a mining threshold; ``lo`` is exclusive, ``hi`` inclusive."""
    if not (lo < value <= hi):
        log_decision(
            "rejected",
            reason=THRESHOLD_OUT_OF_RANGE,
            state={"parameter": name, "value": value, "range": f"({lo}, {hi}]"},
        )
        raise QueryRejected(
            THRESHOLD_OUT_OF_RANGE,
            f"{name} must be in ({lo}, {hi}], got {value}",
        )
    return value
