"""Orchestration: corpus ingest, itemset mining and the audited rule pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

from .apriori import mine_frequent_itemsets
from .config import Settings, get_settings
from .corpus import CorpusSpec, build_corpus
from .diagnostics import DiagnosticCollector
from .indices import FrequentItemsetTable, TransactionIndex
from .mining import AuditThresholds, generate_rules
from .models import Rule, RuleQuery
from .repository import Database
from .validation import QueryValidationError, ValidationIssue, validate_rule_request


@dataclass(frozen=True)
class IngestResult:
    dataset_id: int
    dataset_name: str
    n_transactions: int
    n_itemsets: int
    diagnostics: List[Dict[str, Any]]


@dataclass(frozen=True)
class RuleResult:
    dataset_id: int
    dataset_name: str
    request_id: str
    rules: List[Rule]
    diagnostics: List[Dict[str, Any]]
    query: RuleQuery


class RuleAuditService:
    def __init__(self, db: Database, settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or get_settings()

    def _thresholds(self) -> AuditThresholds:
        return AuditThresholds(
            small_sample_n=self.settings.small_sample_threshold,
            rare_event_count=self.settings.rare_event_threshold,
        )

    def ingest_dataset(
        self,
        name: str,
        raw_transactions: Sequence[Sequence[str]],
        min_support: float,
        *,
        overwrite: bool = False,
        request_id: str | None = None,
        max_length: int | None = None,
    ) -> IngestResult:
        diag = DiagnosticCollector(request_id=request_id, redact=self.settings.redact_pii)
        diag.add("ingest", "started", "ingesting dataset", dataset_name=name,
                 rows=len(raw_transactions), min_support=min_support)

        corpus = build_corpus(
            raw_transactions,
            max_transactions=self.settings.max_transactions,
            max_items_per_transaction=self.settings.max_items_per_transaction,
        )
        diag.add(
            "normalize",
            "accepted",
            "corpus normalized with set semantics; duplicate in-transaction "
            "occurrences collapsed",
            n_transactions=corpus.n_transactions,
            duplicate_occurrences_collapsed=corpus.duplicate_occurrences_collapsed,
            blank_item_tokens_removed=corpus.blank_item_tokens_removed,
            dropped_blank_transactions=len(corpus.dropped_blank_tids),
        )

        index = TransactionIndex.from_corpus(corpus)
        table = mine_frequent_itemsets(index, min_support, max_length=max_length)
        diag.add(
            "mine_itemsets",
            "accepted",
            "frequent itemsets materialized via apriori",
            n_transactions=index.n_transactions,
            n_itemsets=len(table),
            min_support=min_support,
        )

        dataset_id = self.db.save_dataset(name, corpus, overwrite=overwrite)
        self.db.save_itemsets(dataset_id, table)
        diag.add("persist", "accepted", "dataset and itemsets stored",
                 dataset_id=dataset_id)

        return IngestResult(
            dataset_id=dataset_id,
            dataset_name=name,
            n_transactions=corpus.n_transactions,
            n_itemsets=len(table),
            diagnostics=diag.summary(),
        )

    def _resolve_dataset(self, dataset: str | int) -> tuple[int, str]:
        if isinstance(dataset, int):
            dataset_id = dataset
            name = self.db.get_dataset_name(dataset_id)
            if name is None:
                raise KeyError(f"dataset id {dataset_id} not found")
            return dataset_id, name
        dataset_id = self.db.get_dataset_id(dataset)
        if dataset_id is None:
            raise KeyError(f"dataset {dataset!r} not found")
        return dataset_id, dataset

    def audit_rules_raw(
        self,
        dataset: str | int,
        *,
        min_confidence: float = 0.0,
        min_lift: float | None = None,
        min_leverage: float | None = None,
        max_rules: int | None = None,
        antecedent: Any = None,
        consequent: Any = None,
        request_id: str | None = None,
    ) -> RuleResult:
        """Validate a raw rule request, then run the audited generation."""
        diag = DiagnosticCollector(request_id=request_id, redact=self.settings.redact_pii)
        dataset_id, dataset_name = self._resolve_dataset(dataset)

        query, _min_support, issues = validate_rule_request(
            min_confidence=min_confidence,
            min_lift=min_lift,
            min_leverage=min_leverage,
            max_rules=max_rules,
            antecedent=antecedent,
            consequent=consequent,
        )
        if issues:
            diag.add(
                "validate",
                "invalid",
                "request rejected at the boundary",
                dataset_id=dataset_id,
                issues=[{"field": i.field, "reason": i.reason.value} for i in issues],
            )
            self.db.save_audit_run(
                dataset_id,
                diag.request_id,
                params={
                    "min_confidence": min_confidence,
                    "min_lift": min_lift,
                    "min_leverage": min_leverage,
                    "max_rules": max_rules,
                    "antecedent": diag.itemset_view(tuple(antecedent or ())),
                    "consequent": diag.itemset_view(tuple(consequent or ())),
                },
                summary={"outcome": "invalid", "issue_count": len(issues)},
            )
            raise QueryValidationError(issues)

        diag.add(
            "validate",
            "accepted",
            "request parameters within domain",
            min_confidence=query.min_confidence,
            min_lift=query.min_lift,
            min_leverage=query.min_leverage,
            antecedent=diag.itemset_view(query.antecedent) if query.antecedent else None,
            consequent=diag.itemset_view(query.consequent) if query.consequent else None,
        )
        return self._run_generation(dataset_id, dataset_name, query, diag)

    def audit_rules(
        self, dataset: str | int, query: RuleQuery, *, request_id: str | None = None
    ) -> RuleResult:
        """Run an already-constructed query (programmatic callers / tests)."""
        diag = DiagnosticCollector(request_id=request_id, redact=self.settings.redact_pii)
        dataset_id, dataset_name = self._resolve_dataset(dataset)
        return self._run_generation(dataset_id, dataset_name, query, diag)

    def _run_generation(
        self,
        dataset_id: int,
        dataset_name: str,
        query: RuleQuery,
        diag: DiagnosticCollector,
    ) -> RuleResult:
        table = self.db.load_itemsets(dataset_id)
        diag.add(
            "load_itemsets",
            "accepted",
            "frequent-itemset table loaded",
            dataset_id=dataset_id,
            n_transactions=table.n_transactions,
            n_itemsets=len(table),
        )

        # Count resolver keeps results exact even if the stored table lacks
        # some singleton side subsets (externally supplied sparse tables).
        transactions = self.db.load_transactions(dataset_id)
        index = TransactionIndex(transactions, table.n_transactions)

        rules = generate_rules(
            table,
            query,
            thresholds=self._thresholds(),
            count_resolver=index.itemset_count,
        )

        status_counts: Dict[str, int] = {}
        for rule in rules:
            status_counts[rule.status.value] = status_counts.get(rule.status.value, 0) + 1
        diag.add(
            "generate",
            "accepted",
            "rules generated and adjudicated; statuses are per-rule, high "
            "confidence is never treated as causation",
            n_rules=len(rules),
            status_counts=status_counts,
            warning_codes=sorted({w for r in rules for w in r.warnings}),
        )

        summary = {
            "outcome": "ok",
            "n_rules": len(rules),
            "status_counts": status_counts,
        }
        self.db.save_audit_run(
            dataset_id,
            diag.request_id,
            params={
                "min_confidence": query.min_confidence,
                "min_lift": query.min_lift,
                "min_leverage": query.min_leverage,
                "max_rules": query.max_rules,
            },
            summary=summary,
        )
        return RuleResult(
            dataset_id=dataset_id,
            dataset_name=dataset_name,
            request_id=diag.request_id,
            rules=rules,
            diagnostics=diag.summary(),
            query=query,
        )
