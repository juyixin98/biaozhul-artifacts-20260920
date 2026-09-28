"""Corpus specification: what a valid transaction corpus looks like.

Semantics fixed here (and documented in docs/semantics.md):

  * A transaction is a *set* of items. Duplicates inside one transaction
    are collapsed before storage — the same dedup semantics as the
    reference support libraries (mlxtend / efficient-apriori), which
    one-hot encode each transaction. A transaction contributes at most 1
    to any itemset's support count.
  * Items are non-empty strings after stripping; transactions with zero
    distinct items are rejected (they carry no information and would
    silently inflate N).
  * Transaction ids must be unique within a corpus.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MAX_ITEM_LENGTH = 256


class CorpusError(ValueError):
    """Raised when a corpus violates the specification.

    ``category`` is stable and machine-readable so the API layer and tests
    can assert on the failure class, not the message text.
    """

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail


@dataclass
class NormalizedTransaction:
    transaction_id: str
    items: frozenset[str]
    n_items_raw: int
    n_duplicates_removed: int = field(default=0)


def normalize_transaction(transaction_id: str, raw_items: list[str]) -> NormalizedTransaction:
    """Validate and deduplicate one transaction.

    Raises:
        CorpusError: ``EMPTY_ITEM`` if an item is blank,
            ``EMPTY_TRANSACTION`` if no valid items remain,
            ``ITEM_TOO_LONG`` if an item exceeds the length cap.
    """
    if not transaction_id or not transaction_id.strip():
        raise CorpusError("EMPTY_TRANSACTION_ID", "transaction_id must be non-empty")

    cleaned: list[str] = []
    for raw in raw_items:
        item = raw.strip() if isinstance(raw, str) else ""
        if not item:
            raise CorpusError("EMPTY_ITEM", "items must be non-empty strings")
        if len(item) > MAX_ITEM_LENGTH:
            raise CorpusError(
                "ITEM_TOO_LONG", f"item exceeds {MAX_ITEM_LENGTH} characters"
            )
        cleaned.append(item)

    deduped = frozenset(cleaned)
    if not deduped:
        raise CorpusError(
            "EMPTY_TRANSACTION",
            "transaction has no items after normalization",
        )
    return NormalizedTransaction(
        transaction_id=transaction_id.strip(),
        items=deduped,
        n_items_raw=len(cleaned),
        n_duplicates_removed=len(cleaned) - len(deduped),
    )


def normalize_corpus(
    transactions: list[tuple[str, list[str]]],
) -> list[NormalizedTransaction]:
    """Normalize a whole corpus, rejecting duplicate transaction ids."""
    seen: set[str] = set()
    out: list[NormalizedTransaction] = []
    for tid, items in transactions:
        norm = normalize_transaction(tid, items)
        if norm.transaction_id in seen:
            raise CorpusError(
                "DUPLICATE_TRANSACTION_ID",
                f"transaction id occurs more than once: {norm.transaction_id!r}",
            )
        seen.add(norm.transaction_id)
        out.append(norm)
    if not out:
        raise CorpusError("EMPTY_CORPUS", "corpus must contain at least one transaction")
    return out
