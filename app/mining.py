"""Mining kernel: rule metrics, enumeration, audit status and warnings.

Metric definitions (n = number of transactions; counts are set-based, so a
repeated item in one transaction counts once):

    support(A->C)     = |A u C| / n
    confidence(A->C)  = |A u C| / |A|              undefined if |A| = 0
    lift(A->C)        = supp(A u C) / (supp(A) supp(C))
                      = n * |A u C| / (|A| * |C|)   undefined if |A|*|C| = 0
    leverage(A->C)    = supp(A u C) - supp(A) supp(C)

Interpretation boundaries:
    lift > 1  positive association; lift = 1 independence; lift < 1 mutual
    antagonism.  High confidence alone is NOT evidence of association (a
    universal consequent yields confidence 1 with lift 1) and never implies
    causation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Sequence, Tuple

from .indices import FrequentItemsetTable
from .models import (
    Itemset,
    Rule,
    RuleMetrics,
    RuleQuery,
    RuleStatus,
    WarningCode,
    split_itemsets,
)

# A confidence at or above this while lift <= 1 triggers the
# "high confidence is not causation / not even association" warning.
HIGH_CONFIDENCE_NOTE = 0.8
_EPS = 1e-12


@dataclass(frozen=True)
class AuditThresholds:
    small_sample_n: int = 30
    rare_event_count: int = 5


def compute_metrics(
    n_transactions: int,
    count_union: int,
    count_antecedent: int,
    count_consequent: int,
) -> RuleMetrics:
    """Compute the four metrics from raw counts.

    Pure function: every value is derived from the four integer inputs, so
    hand-computed expectations can be asserted directly.

    Undefined semantics are explicit:
      * ``confidence`` is None with reason when |A| = 0;
      * ``lift`` is None with reason when |A|*|C| = 0;
      * leverage stays defined whenever n > 0 even if a side count is zero.
    """
    if n_transactions <= 0:
        raise ValueError("n_transactions must be positive")

    n = n_transactions
    supp_u = count_union / n
    supp_a = count_antecedent / n
    supp_c = count_consequent / n

    undefined_reason: str | None = None
    confidence: float | None = None
    lift: float | None = None
    lift_denom: float | None = None

    if count_antecedent == 0:
        undefined_reason = (
            "confidence and lift undefined: antecedent support count is 0 "
            "(division by zero)"
        )
        confidence = None
        lift = None
        lift_denom = 0.0 if count_consequent == 0 else supp_a * supp_c
    elif count_consequent == 0:
        undefined_reason = (
            "lift undefined: consequent support count is 0 (division by zero); "
            "confidence is 0 because the union cannot occur"
        )
        confidence = 0.0
        lift = None
        lift_denom = 0.0
    else:
        confidence = count_union / count_antecedent
        lift_denom = supp_a * supp_c
        lift = supp_u / lift_denom

    leverage = supp_u - supp_a * supp_c

    return RuleMetrics(
        support_count=count_union,
        support=supp_u,
        confidence=confidence,
        lift=lift,
        leverage=leverage,
        antecedent_count=count_antecedent,
        consequent_count=count_consequent,
        antecedent_support=supp_a,
        consequent_support=supp_c,
        confidence_denominator=count_antecedent,
        lift_denominator=lift_denom,
        defined=undefined_reason is None,
        undefined_reason=undefined_reason,
    )


def _build_warnings(
    metrics: RuleMetrics, n_transactions: int, thresholds: AuditThresholds
) -> Tuple[List[str], List[str]]:
    """Return (warnings, evidence_notes). Warnings always appear in results."""
    warnings: List[str] = []
    notes: List[str] = []

    if n_transactions < thresholds.small_sample_n:
        warnings.append(WarningCode.SMALL_SAMPLE.value)
        notes.append(
            f"sample size n={n_transactions} is below the small-sample "
            f"threshold {thresholds.small_sample_n}; point estimates are unstable"
        )

    if metrics.antecedent_count < thresholds.rare_event_count:
        warnings.append(WarningCode.RARE_ANTECEDENT.value)
        notes.append(
            f"antecedent occurred only {metrics.antecedent_count} time(s) "
            f"(rare-event threshold {thresholds.rare_event_count})"
        )
    if metrics.consequent_count < thresholds.rare_event_count:
        warnings.append(WarningCode.RARE_CONSEQUENT.value)
        notes.append(
            f"consequent occurred only {metrics.consequent_count} time(s) "
            f"(rare-event threshold {thresholds.rare_event_count})"
        )
    if metrics.support_count < thresholds.rare_event_count:
        warnings.append(WarningCode.RARE_RULE.value)
        notes.append(
            f"rule co-occurrence count {metrics.support_count} is below the "
            f"rare-event threshold {thresholds.rare_event_count}"
        )

    if metrics.confidence is not None and metrics.confidence >= 1.0 - _EPS:
        warnings.append(WarningCode.PERFECT_DETERMINISM.value)
        notes.append(
            "confidence is exactly 1.0 in this sample; this may be a ceiling "
            "artifact of a small or sparse corpus"
        )

    if (
        metrics.confidence is not None
        and metrics.confidence >= HIGH_CONFIDENCE_NOTE
        and metrics.lift is not None
        and metrics.lift <= 1.0 + _EPS
    ):
        warnings.append(WarningCode.LIFT_NOT_CAUSATION.value)
        notes.append(
            "confidence is high but lift <= 1: the apparent predictability is "
            "explained by the consequent's own prevalence, not by a positive "
            "association; confidence/lift never establish causation"
        )

    return warnings, notes


def _adjudicate(metrics: RuleMetrics, query: RuleQuery) -> Tuple[RuleStatus, List[str]]:
    """Decide accept / reject against explicit thresholds.

    Threshold failures are REJECTED with named reasons — never silently
    filtered — so an audit can explain every non-accepted rule.
    """
    reasons: List[str] = []

    if query.min_confidence > 0.0:
        if metrics.confidence is None:
            reasons.append("min_confidence cannot be applied: confidence undefined")
        elif metrics.confidence < query.min_confidence - _EPS:
            reasons.append(
                f"confidence {metrics.confidence:.6f} below min_confidence "
                f"{query.min_confidence:.6f}"
            )

    if query.min_lift is not None:
        if metrics.lift is None:
            reasons.append("min_lift cannot be applied: lift undefined")
        elif metrics.lift < query.min_lift - _EPS:
            reasons.append(
                f"lift {metrics.lift:.6f} below min_lift {query.min_lift:.6f}"
            )

    if query.min_leverage is not None and metrics.leverage < query.min_leverage - _EPS:
        reasons.append(
            f"leverage {metrics.leverage:.6f} below min_leverage "
            f"{query.min_leverage:.6f}"
        )

    if reasons:
        return RuleStatus.REJECTED, reasons
    return RuleStatus.ACCEPTED, []


def build_rule(
    antecedent: Itemset,
    consequent: Itemset,
    metrics: RuleMetrics,
    query: RuleQuery,
    n_transactions: int,
    thresholds: AuditThresholds,
) -> Rule:
    warnings, _notes = _build_warnings(metrics, n_transactions, thresholds)

    if not metrics.defined:
        return Rule(
            antecedent,
            consequent,
            metrics,
            RuleStatus.UNDEFINED,
            reasons=[metrics.undefined_reason or "metric undefined"],
            warnings=tuple(warnings),
        )

    status, reasons = _adjudicate(metrics, query)

    # Objective threshold failure wins: the rule really is below the bar.
    # Otherwise, a rare co-occurrence makes a positive-audit verdict
    # unsupported even though the point estimate passes — indeterminate.
    if status == RuleStatus.ACCEPTED and (
        metrics.support_count < thresholds.rare_event_count
        or n_transactions < thresholds.small_sample_n
    ):
        return Rule(
            antecedent,
            consequent,
            metrics,
            RuleStatus.INDETERMINATE,
            reasons=[
                "metric is defined and passes thresholds, but evidence is too "
                "sparse (rare co-occurrence and/or small sample) to confirm a signal"
            ],
            warnings=tuple(warnings),
        )

    return Rule(
        antecedent,
        consequent,
        metrics,
        status,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
    )


CountResolver = Callable[[Itemset], int]


def generate_rules(
    table: FrequentItemsetTable,
    query: RuleQuery | None = None,
    *,
    thresholds: AuditThresholds | None = None,
    count_resolver: CountResolver | None = None,
) -> List[Rule]:
    """Enumerate every A->C bipartition of every frequent itemset (size >= 2).

    Counts come from the frequent-itemset table (downward closure guarantees
    every side is present). ``count_resolver`` is an optional fallback used
    when the table was supplied without all subsets; without it, a missing
    subset raises KeyError rather than silently fabricating a count.
    """
    query = query or RuleQuery()
    thresholds = thresholds or AuditThresholds()
    n = table.n_transactions

    pairs: List[Tuple[Itemset, Itemset]] = split_itemsets(table.itemsets())

    # Apply optional side restrictions before metric computation.
    if query.antecedent is not None:
        wanted = tuple(sorted(query.antecedent))
        pairs = [p for p in pairs if p[0] == wanted]
    if query.consequent is not None:
        wanted = tuple(sorted(query.consequent))
        pairs = [p for p in pairs if p[1] == wanted]

    pairs.sort()

    rules: List[Rule] = []
    for a, c in pairs:
        union = tuple(sorted(set(a) | set(c)))
        fi_union = table.get(union)
        fi_a = table.get(a)
        fi_c = table.get(c)
        if fi_union is None or fi_a is None or fi_c is None:
            if count_resolver is None:
                raise KeyError(
                    f"frequent-itemset table is missing a subset required for "
                    f"rule {a} -> {c}; supply complete itemsets or a count resolver"
                )
            count_u = count_resolver(union) if fi_union is None else fi_union.support_count
            count_a = count_resolver(a) if fi_a is None else fi_a.support_count
            count_c = count_resolver(c) if fi_c is None else fi_c.support_count
        else:
            count_u, count_a, count_c = (
                fi_union.support_count,
                fi_a.support_count,
                fi_c.support_count,
            )

        metrics = compute_metrics(n, count_u, count_a, count_c)
        rules.append(build_rule(a, c, metrics, query, n, thresholds))

    if query.max_rules is not None:
        rules = rules[: query.max_rules]
    return rules
