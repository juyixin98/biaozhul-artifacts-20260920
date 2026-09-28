"""Framework-neutral domain model.

These dataclasses intentionally have no FastAPI/pydantic dependency so the
mining kernel can be exercised and reused outside the web layer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Iterable, List, Sequence, Tuple


class RuleStatus(str, Enum):
    """Audit verdict for a generated rule."""

    ACCEPTED = "accepted"
    """Passed every requested threshold and metric is defined."""

    REJECTED = "rejected"
    """Failed an explicit, well-defined threshold (e.g. min confidence)."""

    UNDEFINED = "undefined"
    """A denominator is zero, so the requested metric does not exist."""

    INVALID = "invalid"
    """The query itself is malformed or out of range."""

    INDETERMINATE = "indeterminate"
    """Metric is defined but evidence is too weak to audit as a real signal."""


class WarningCode(str, Enum):
    SMALL_SAMPLE = "small_sample"
    RARE_ANTECEDENT = "rare_antecedent"
    RARE_CONSEQUENT = "rare_consequent"
    RARE_RULE = "rare_rule"
    PERFECT_DETERMINISM = "perfect_determinism"
    LIFT_NOT_CAUSATION = "lift_not_causation"


# Threshold ranges enforced at the system boundary.
MIN_MIN_SUPPORT: Final[float] = 0.0
MAX_MIN_SUPPORT: Final[float] = 1.0
MIN_MIN_CONFIDENCE: Final[float] = 0.0
MAX_MIN_CONFIDENCE: Final[float] = 1.0
MIN_MIN_LIFT: Final[float] = 0.0
MAX_MIN_LIFT: Final[float] = math.inf

Itemset = Tuple[str, ...]


def normalize_items(items: Iterable[str]) -> Itemset:
    """Normalize a raw item collection to a canonical in-transaction itemset.

    Semantics, aligned with standard support libraries (e.g. mlxtend):
      * a transaction is a *set*: duplicate occurrences of an item inside one
        transaction count once;
      * items are compared by their exact string value after stripping
        surrounding whitespace; blank/empty items are dropped at parse time
        and reported separately by the corpus parser;
      * the canonical form is a sorted tuple so equal itemsets hash/eq equal.
    """
    cleaned = {item.strip() for item in items if item is not None and item.strip()}
    return tuple(sorted(cleaned))


@dataclass(frozen=True)
class Transaction:
    tid: int
    items: Itemset


@dataclass(frozen=True)
class FrequentItemset:
    items: Itemset
    support_count: int
    support: float  # relative support: support_count / n_transactions

    def __post_init__(self) -> None:
        if self.items != tuple(sorted(self.items)):
            object.__setattr__(self, "items", tuple(sorted(self.items)))


@dataclass(frozen=True)
class RuleMetrics:
    support_count: int
    support: float
    confidence: float | None
    lift: float | None
    leverage: float | None
    antecedent_count: int
    consequent_count: int

    # numerator/denominator audit fields
    antecedent_support: float
    consequent_support: float
    confidence_denominator: int  # |antecedent|
    lift_denominator: float | None  # supp(A)*supp(C)
    defined: bool
    undefined_reason: str | None = None


@dataclass(frozen=True)
class Rule:
    antecedent: Itemset
    consequent: Itemset
    metrics: RuleMetrics
    status: RuleStatus
    reasons: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RuleQuery:
    min_confidence: float = 0.0
    min_lift: float | None = None
    min_leverage: float | None = None
    max_rules: int | None = None
    antecedent: Itemset | None = None
    consequent: Itemset | None = None


def split_itemsets(itemsets: Sequence[Itemset]) -> List[Tuple[Itemset, Itemset]]:
    """Return all non-empty proper bipartitions A -> C of each itemset.

    A and C are disjoint, both non-empty, and their union is the itemset.
    Itemsets of size < 2 produce no rules.
    """
    out: List[Tuple[Itemset, Itemset]] = []
    for items in itemsets:
        n = len(items)
        if n < 2:
            continue
        for mask in range(1, (1 << n) - 1):
            left = tuple(items[i] for i in range(n) if mask & (1 << i))
            right = tuple(items[i] for i in range(n) if not (mask & (1 << i)))
            out.append((left, right))
    return out
