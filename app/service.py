"""Application service: orchestrates store, mining kernel and validation.

Both the FastAPI layer and scripts/verify.py drive the pipeline through
this module, so HTTP is a thin adapter and every behavior is testable
without a server.
"""
from __future__ import annotations

import json

from .config import Settings
from .corpus.loader import LoadedCorpus, ingest_corpus
from .diagnostics import log_decision, mask_items
from .index.db import Store
from .mining.itemsets import mine_frequent_itemsets
from .mining.rules import (
    Rule,
    attach_warnings,
    evaluate_rule,
    generate_rules,
)
from .validation import queries
from .validation.queries import QueryRejected, validate_rule_sides, validate_threshold


class AuditService:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    # -- corpus ------------------------------------------------------------

    def create_corpus(
        self, name: str, transactions: list[tuple[str, list[str]]]
    ) -> LoadedCorpus:
        return ingest_corpus(self.store, name, transactions)

    def _require_corpus(self, corpus_id: int) -> int:
        n = self.store.transaction_count(corpus_id)
        if n == 0:
            log_decision(
                "rejected", reason=queries.CORPUS_NOT_FOUND,
                record_id=f"corpus:{corpus_id}",
            )
            raise QueryRejected(
                queries.CORPUS_NOT_FOUND, f"corpus {corpus_id} not found or empty"
            )
        return n

    # -- mining ------------------------------------------------------------

    def mine(self, corpus_id: int, min_support: float) -> list[dict]:
        validate_threshold("min_support", min_support, lo=0.0, hi=1.0)
        n = self._require_corpus(corpus_id)
        itemsets, _ = mine_frequent_itemsets(
            self.store.iter_transaction_itemsets(corpus_id), min_support
        )
        self.store.replace_itemsets(
            corpus_id,
            ((fi.items, fi.support_count, fi.support) for fi in itemsets),
            min_support,
        )
        log_decision(
            "accepted",
            reason="ITEMSETS_MINED",
            record_id=f"corpus:{corpus_id}",
            state={"min_support": min_support, "n_itemsets": len(itemsets), "n": n},
        )
        return [
            {
                "items": sorted(fi.items),
                "support_count": fi.support_count,
                "support": fi.support,
            }
            for fi in itemsets
        ]

    # -- rules -------------------------------------------------------------

    def _itemset_counts(self, corpus_id: int) -> dict[frozenset[str], int]:
        rows = self.store.get_itemsets(corpus_id)
        return {frozenset(json.loads(r["items_json"])): int(r["support_count"]) for r in rows}

    def _warn(self, rule: Rule, n: int, joint_count: int, cons_support: float) -> None:
        attach_warnings(
            rule,
            n_transactions=n,
            joint_count=joint_count,
            consequent_support=cons_support,
            small_sample_threshold=self.settings.small_sample_threshold,
            rare_event_count_threshold=self.settings.rare_event_count_threshold,
            ubiquitous_support_threshold=self.settings.ubiquitous_support_threshold,
        )

    def generate_rules(
        self, corpus_id: int, min_confidence: float, min_lift: float | None
    ) -> list[Rule]:
        validate_threshold("min_confidence", min_confidence, lo=0.0, hi=1.0)
        if min_lift is not None and min_lift <= 0.0:
            raise QueryRejected(
                queries.THRESHOLD_OUT_OF_RANGE, "min_lift must be positive"
            )
        n = self._require_corpus(corpus_id)
        counts = self._itemset_counts(corpus_id)
        if not counts:
            raise QueryRejected(
                "ITEMSETS_NOT_MINED",
                f"corpus {corpus_id} has no mined itemsets; call /mine first",
            )
        rules = generate_rules(counts, n, min_confidence, min_lift)
        for rule in rules:
            joint = counts[rule.antecedent | rule.consequent]
            self._warn(rule, n, joint, counts[rule.consequent] / n)
        self.store.replace_rules(
            corpus_id,
            (
                {
                    "rule_id": r.rule_id,
                    "antecedent": r.antecedent,
                    "consequent": r.consequent,
                    "support": r.metrics.support,
                    "confidence": r.metrics.confidence,
                    "lift": r.metrics.lift,
                    "leverage": r.metrics.leverage,
                    "warnings": r.warnings,
                }
                for r in rules
            ),
        )
        log_decision(
            "accepted",
            reason="RULES_GENERATED",
            record_id=f"corpus:{corpus_id}",
            state={
                "min_confidence": min_confidence,
                "min_lift": min_lift,
                "n_rules": len(rules),
            },
        )
        return rules

    def evaluate(self, corpus_id: int, antecedent: list[str], consequent: list[str]) -> Rule:
        n = self._require_corpus(corpus_id)
        sides = validate_rule_sides(
            antecedent,
            consequent,
            mask_items_in_logs=self.settings.mask_items_in_logs,
        )
        rule, count_a, count_b = evaluate_rule(
            lambda items: self.store.support_count(corpus_id, items),
            n,
            sides.antecedent,
            sides.consequent,
        )
        joint = self.store.support_count(corpus_id, sides.antecedent | sides.consequent)
        self._warn(rule, n, joint, count_b / n)

        if rule.metrics.confidence is None or rule.metrics.lift is None:
            log_decision(
                "undecidable",
                reason="UNDEFINED_ZERO_DENOMINATOR",
                record_id=f"corpus:{corpus_id}",
                state={
                    "antecedent": mask_items(
                        sides.antecedent, enabled=self.settings.mask_items_in_logs
                    ),
                    "count_antecedent": count_a,
                    "count_consequent": count_b,
                    "n": n,
                },
            )
        else:
            log_decision(
                "accepted",
                reason="RULE_EVALUATED",
                record_id=f"corpus:{corpus_id}",
                state={
                    "count_antecedent": count_a,
                    "count_consequent": count_b,
                    "confidence": rule.metrics.confidence,
                    "lift": rule.metrics.lift,
                },
            )
        return rule
