"""Association rule generation and metric computation.

Metrics (N = number of transactions, counts are transaction counts):

  support(X->Y)   = count(X ∪ Y) / N
  confidence      = count(X ∪ Y) / count(X)        -- undefined if count(X) == 0
  lift            = confidence / (count(Y) / N)    -- undefined if count(Y) == 0
                    or if confidence is undefined
  leverage        = support(X ∪ Y) - support(X) * support(Y)

Boundary semantics (see docs/semantics.md):
  * A zero denominator makes the metric *undefined* (None), never 0 or NaN.
  * Empty antecedent/consequent are rejected upstream in app.validation,
    not silently treated as 1-itemsets.
  * High confidence is NOT treated as evidence of causation: rules whose
    lift <= 1 carry a HIGH_CONFIDENCE_LOW_LIFT warning, and consequents
    present in nearly every transaction carry UBIQUITOUS_CONSEQUENT.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import combinations

# Warning codes (stable, asserted on by tests).
W_SMALL_SAMPLE = "SMALL_SAMPLE"
W_RARE_EVENT = "RARE_EVENT"
W_UBIQUITOUS_CONSEQUENT = "UBIQUITOUS_CONSEQUENT"
W_HIGH_CONF_LOW_LIFT = "HIGH_CONFIDENCE_LOW_LIFT"

# Confidence at or above this is "high" for the no-causation warning.
# Below it a low lift is unremarkable and the warning would be noise.
HIGH_CONFIDENCE_THRESHOLD = 0.8


@dataclass(frozen=True)
class RuleMetrics:
    support: float
    confidence: float | None  # None => undefined (zero denominator)
    lift: float | None
    leverage: float


def compute_metrics(
    count_ab: int, count_a: int, count_b: int, n: int
) -> RuleMetrics:
    """Compute rule metrics from raw counts; zero denominators -> None.

    Each metric is a single division of exact integers, so results are
    correctly rounded and bit-identical to ``float(Fraction(...))`` of the
    exact rational — this is what lets tests assert exact equality against
    the independent Fraction-based reference implementation.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    support = count_ab / n
    confidence = count_ab / count_a if count_a > 0 else None
    if confidence is None or count_b == 0:
        lift = None
    else:
        # confidence / (count_b / n) == (count_ab * n) / (count_a * count_b)
        lift = (count_ab * n) / (count_a * count_b)
    # support(ab) - support(a) * support(b), as one exact rational
    leverage = (count_ab * n - count_a * count_b) / (n * n)
    return RuleMetrics(
        support=support, confidence=confidence, lift=lift, leverage=leverage
    )


@dataclass
class Rule:
    antecedent: frozenset[str]
    consequent: frozenset[str]
    metrics: RuleMetrics
    warnings: list[str] = field(default_factory=list)

    @property
    def rule_id(self) -> str:
        return json.dumps([sorted(self.antecedent), sorted(self.consequent)])


def attach_warnings(
    rule: Rule,
    *,
    n_transactions: int,
    joint_count: int,
    consequent_support: float,
    small_sample_threshold: int,
    rare_event_count_threshold: int,
    ubiquitous_support_threshold: float,
) -> None:
    """Populate rule.warnings in-place from corpus- and rule-level state."""
    warnings: list[str] = []
    if n_transactions < small_sample_threshold:
        warnings.append(W_SMALL_SAMPLE)
    if joint_count < rare_event_count_threshold:
        warnings.append(W_RARE_EVENT)
    if consequent_support >= ubiquitous_support_threshold:
        warnings.append(W_UBIQUITOUS_CONSEQUENT)
    if (
        rule.metrics.confidence is not None
        and rule.metrics.confidence >= HIGH_CONFIDENCE_THRESHOLD
        and rule.metrics.lift is not None
        and rule.metrics.lift <= 1.0
    ):
        # High confidence with non-positive association: the confidence is
        # explained by consequent prevalence, not by the antecedent. Must
        # not be read as causal or even associative.
        warnings.append(W_HIGH_CONF_LOW_LIFT)
    rule.warnings = warnings


def _merge_consequents(
    consequents: list[frozenset[str]], size: int
) -> list[frozenset[str]]:
    """Apriori-style join of passing consequents to the next size."""
    out: set[frozenset[str]] = set()
    ordered = sorted(consequents, key=lambda s: sorted(s))
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            union = ordered[i] | ordered[j]
            if len(union) == size:
                out.add(union)
    return sorted(out, key=lambda s: sorted(s))


def generate_rules(
    itemsets: dict[frozenset[str], int],
    n_transactions: int,
    min_confidence: float,
    min_lift: float | None = None,
) -> list[Rule]:
    """Generate rules from frequent itemsets with confidence pruning.

    ``itemsets`` maps each frequent itemset to its support *count*.

    Pruning: for a fixed itemset Z, confidence of X->Y (X ∪ Y = Z) is
    count(Z)/count(X). Shrinking the consequent enlarges the antecedent,
    which can only *lower* count(X), i.e. raise confidence. Hence once a
    consequent fails min_confidence, no superset consequent can pass, and
    we only merge *passing* consequents — the standard Apriori rule
    pruning, which provably misses no rule (verified against brute force
    in tests/test_pruning_completeness.py).
    """
    if not 0.0 < min_confidence <= 1.0:
        raise ValueError(f"min_confidence must be in (0, 1], got {min_confidence}")
    if min_lift is not None and min_lift <= 0.0:
        raise ValueError(f"min_lift must be positive, got {min_lift}")

    rules: list[Rule] = []
    for itemset, count_ab in itemsets.items():
        if len(itemset) < 2:
            continue
        # Candidate consequents of size 1, merged upward while they pass.
        consequents = [frozenset([i]) for i in sorted(itemset)]
        size = 1
        while consequents and size < len(itemset):
            passing: list[frozenset[str]] = []
            for cons in consequents:
                ante = itemset - cons
                metrics = compute_metrics(
                    count_ab, itemsets[ante], itemsets[cons], n_transactions
                )
                assert metrics.confidence is not None  # frequent => count > 0
                if metrics.confidence >= min_confidence:
                    if min_lift is None or (
                        metrics.lift is not None and metrics.lift >= min_lift
                    ):
                        rules.append(Rule(ante, cons, metrics))
                    passing.append(cons)
            size += 1
            consequents = _merge_consequents(passing, size)
    rules.sort(key=lambda r: (sorted(r.antecedent), sorted(r.consequent)))
    return rules


def evaluate_rule(
    count_fn: Callable[[frozenset[str]], int],
    n_transactions: int,
    antecedent: frozenset[str],
    consequent: frozenset[str],
) -> tuple[Rule, int, int]:
    """Evaluate one arbitrary rule against a corpus via ``count_fn``.

    Returns the rule plus the antecedent and consequent counts so callers
    can log the key state. Confidence/lift are None when their denominator
    is zero (undefined, not zero).
    """
    count_a = count_fn(antecedent)
    count_b = count_fn(consequent)
    count_ab = count_fn(antecedent | consequent)
    metrics = compute_metrics(count_ab, count_a, count_b, n_transactions)
    return Rule(antecedent, consequent, metrics), count_a, count_b


def all_rules_bruteforce(
    itemsets: dict[frozenset[str], int],
    n_transactions: int,
    min_confidence: float,
) -> list[Rule]:
    """Exhaustive rule enumeration with no pruning.

    Exposed for the completeness cross-check (tests and scripts/verify.py
    compare the pruned generator against this on every fixture).
    """
    rules: list[Rule] = []
    for itemset, count_ab in itemsets.items():
        if len(itemset) < 2:
            continue
        items = sorted(itemset)
        for r in range(1, len(items)):
            for cons_tuple in combinations(items, r):
                cons = frozenset(cons_tuple)
                ante = itemset - cons
                metrics = compute_metrics(
                    count_ab, itemsets[ante], itemsets[cons], n_transactions
                )
                if metrics.confidence is not None and metrics.confidence >= min_confidence:
                    rules.append(Rule(ante, cons, metrics))
    rules.sort(key=lambda r: (sorted(r.antecedent), sorted(r.consequent)))
    return rules
