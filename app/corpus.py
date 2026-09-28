"""Corpus specification and normalization.

A *corpus* is an ordered list of transactions.  Each raw transaction is a
sequence of item strings.  Normalization applies the same in-transaction
deduplication semantics as standard support libraries (a repeated item in one
transaction contributes one, not two).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Sequence

from .models import Itemset, Transaction, normalize_items


@dataclass(frozen=True)
class CorpusParseIssue:
    tid: int
    code: str
    detail: str  # never contains raw item text in redacted callers; detail is structural here


@dataclass
class CorpusSpec:
    """Validated corpus ready for indexing."""

    transactions: List[Transaction]
    dropped_blank_tids: List[int] = field(default_factory=list)
    duplicate_occurrences_collapsed: int = 0
    blank_item_tokens_removed: int = 0
    issues: List[CorpusParseIssue] = field(default_factory=list)

    @property
    def n_transactions(self) -> int:
        return len(self.transactions)

    def item_count(self) -> int:
        return sum(len(t.items) for t in self.transactions)


def build_corpus(
    raw_transactions: Iterable[Sequence[str]],
    *,
    max_transactions: int = 100_000,
    max_items_per_transaction: int = 1_000,
    drop_empty_transactions: bool = True,
) -> CorpusSpec:
    """Parse and normalize raw transaction rows into a :class:`CorpusSpec`.

    Boundary checks:
      * number of transactions must be within [1, max_transactions];
      * per-transaction *distinct* item count must be <= max_items_per_transaction;
      * empty transactions (or rows that contain only blanks) are either dropped
        (default, matching one-hot market-basket semantics) or rejected;
      * repeated item occurrences inside a row are collapsed and counted.
    """
    spec = CorpusSpec(transactions=[])
    rows = list(raw_transactions)
    if len(rows) > max_transactions:
        raise ValueError(
            f"corpus has {len(rows)} transactions, exceeds max {max_transactions}"
        )

    for tid, raw in enumerate(rows):
        if raw is None:
            if drop_empty_transactions:
                spec.dropped_blank_tids.append(tid)
                spec.issues.append(CorpusParseIssue(tid, "empty_transaction_dropped", "row was null"))
                continue
            raise ValueError(f"transaction {tid} is null")

        blanks = sum(1 for x in raw if x is None or not str(x).strip())
        spec.blank_item_tokens_removed += blanks

        before = sum(1 for x in raw if x is not None and str(x).strip())
        items: Itemset = normalize_items(str(x) for x in raw if x is not None)
        after = len(items)
        # Duplicate occurrences = non-blank raw tokens minus distinct items.
        spec.duplicate_occurrences_collapsed += max(0, before - after)

        if not items:
            if drop_empty_transactions:
                spec.dropped_blank_tids.append(tid)
                spec.issues.append(
                    CorpusParseIssue(tid, "empty_transaction_dropped", "row normalized to empty set")
                )
                continue
            raise ValueError(f"transaction {tid} normalized to an empty itemset")

        if len(items) > max_items_per_transaction:
            raise ValueError(
                f"transaction {tid} has {len(items)} distinct items, "
                f"exceeds max {max_items_per_transaction}"
            )
        spec.transactions.append(Transaction(tid=tid, items=items))

    if spec.n_transactions == 0:
        raise ValueError("corpus contains no non-empty transactions")
    return spec
