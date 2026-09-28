"""Load a validated corpus into the SQLite store."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..diagnostics import log_decision
from ..index.db import Store
from .schema import NormalizedTransaction, normalize_corpus


@dataclass
class LoadedCorpus:
    corpus_id: int
    name: str
    n_transactions: int
    n_distinct_items: int
    n_duplicate_items_removed: int


def load_fixture(path: str | Path) -> tuple[str, list[tuple[str, list[str]]]]:
    """Read a JSON fixture: ``{"name": ..., "transactions": [{"transaction_id": ..., "items": [...]}]}``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    name = data.get("name", Path(path).stem)
    transactions = [(t["transaction_id"], t["items"]) for t in data["transactions"]]
    return name, transactions


def ingest_corpus(
    store: Store,
    name: str,
    transactions: list[tuple[str, list[str]]],
) -> LoadedCorpus:
    """Normalize, persist and index a corpus; returns load statistics."""
    normalized: list[NormalizedTransaction] = normalize_corpus(transactions)

    corpus_id = store.create_corpus(name, len(normalized))
    store.insert_transactions(
        corpus_id,
        (
            (t.transaction_id, t.n_items_raw, len(t.items), sorted(t.items))
            for t in normalized
        ),
    )

    distinct_items = set().union(*(t.items for t in normalized))
    duplicates_removed = sum(t.n_duplicates_removed for t in normalized)
    log_decision(
        "accepted",
        reason="CORPUS_INGESTED",
        record_id=f"corpus:{corpus_id}",
        state={
            "n_transactions": len(normalized),
            "n_distinct_items": len(distinct_items),
            "n_duplicate_items_removed": duplicates_removed,
        },
    )
    return LoadedCorpus(
        corpus_id=corpus_id,
        name=name,
        n_transactions=len(normalized),
        n_distinct_items=len(distinct_items),
        n_duplicate_items_removed=duplicates_removed,
    )
