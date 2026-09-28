"""SQLite persistence for corpora, frequent itemsets and audit runs.

Schema (normalized, no raw PII constraints beyond what callers store):
    datasets(id, name, n_transactions, created_at, corpus_meta_json)
    transactions(dataset_id, tid, position)
    transaction_items(dataset_id, tid, item)
    itemsets(dataset_id, items_json, size, support_count, support, PRIMARY KEY(dataset_id, items_json))
    audit_runs(id, dataset_id, request_id, params_json, created_at, summary_json)
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Sequence, Tuple

from .corpus import CorpusSpec
from .indices import FrequentItemsetTable
from .models import FrequentItemset, Itemset, Transaction

_SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    n_transactions INTEGER NOT NULL,
    created_at REAL NOT NULL,
    corpus_meta_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    tid INTEGER NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (dataset_id, tid)
);
CREATE TABLE IF NOT EXISTS transaction_items (
    dataset_id INTEGER NOT NULL,
    tid INTEGER NOT NULL,
    item TEXT NOT NULL,
    PRIMARY KEY (dataset_id, tid, item)
);
CREATE TABLE IF NOT EXISTS itemsets (
    dataset_id INTEGER NOT NULL,
    items_json TEXT NOT NULL,
    size INTEGER NOT NULL,
    support_count INTEGER NOT NULL,
    support REAL NOT NULL,
    PRIMARY KEY (dataset_id, items_json)
);
CREATE TABLE IF NOT EXISTS audit_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    params_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def save_dataset(
        self, name: str, corpus: CorpusSpec, overwrite: bool = False
    ) -> int:
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM datasets WHERE name = ?", (name,)
            ).fetchone()
            if existing is not None:
                if not overwrite:
                    raise ValueError(f"dataset {name!r} already exists")
                dataset_id = existing["id"]
                conn.execute("DELETE FROM transaction_items WHERE dataset_id = ?", (dataset_id,))
                conn.execute("DELETE FROM transactions WHERE dataset_id = ?", (dataset_id,))
                conn.execute("DELETE FROM itemsets WHERE dataset_id = ?", (dataset_id,))
                conn.execute(
                    "UPDATE datasets SET n_transactions=?, created_at=?, corpus_meta_json=? WHERE id=?",
                    (
                        corpus.n_transactions,
                        time.time(),
                        json.dumps(
                            {
                                "dropped_blank_tids": corpus.dropped_blank_tids,
                                "duplicate_occurrences_collapsed": corpus.duplicate_occurrences_collapsed,
                                "blank_item_tokens_removed": corpus.blank_item_tokens_removed,
                                "issue_count": len(corpus.issues),
                            }
                        ),
                    ),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO datasets(name, n_transactions, created_at, corpus_meta_json)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        name,
                        corpus.n_transactions,
                        time.time(),
                        json.dumps(
                            {
                                "dropped_blank_tids": corpus.dropped_blank_tids,
                                "duplicate_occurrences_collapsed": corpus.duplicate_occurrences_collapsed,
                                "blank_item_tokens_removed": corpus.blank_item_tokens_removed,
                                "issue_count": len(corpus.issues),
                            }
                        ),
                    ),
                )
                dataset_id = cur.lastrowid

            conn.executemany(
                "INSERT INTO transactions(dataset_id, tid, position) VALUES (?, ?, ?)",
                [(dataset_id, t.tid, pos) for pos, t in enumerate(corpus.transactions)],
            )
            conn.executemany(
                "INSERT INTO transaction_items(dataset_id, tid, item) VALUES (?, ?, ?)",
                [
                    (dataset_id, t.tid, item)
                    for t in corpus.transactions
                    for item in t.items
                ],
            )
        return dataset_id

    def load_transactions(self, dataset_id: int) -> List[Transaction]:
        rows = self._conn.execute(
            "SELECT tid, item FROM transaction_items WHERE dataset_id = ? ORDER BY tid, item",
            (dataset_id,),
        ).fetchall()
        by_tid: Dict[int, List[str]] = {}
        for row in rows:
            by_tid.setdefault(row["tid"], []).append(row["item"])
        return [Transaction(tid=tid, items=tuple(items)) for tid, items in sorted(by_tid.items())]

    def dataset_exists(self, name: str) -> bool:
        return (
            self._conn.execute("SELECT 1 FROM datasets WHERE name = ?", (name,)).fetchone()
            is not None
        )

    def get_dataset_id(self, name: str) -> int | None:
        row = self._conn.execute("SELECT id FROM datasets WHERE name = ?", (name,)).fetchone()
        return None if row is None else int(row["id"])

    def get_dataset_name(self, dataset_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT name FROM datasets WHERE id = ?", (dataset_id,)
        ).fetchone()
        return None if row is None else str(row["name"])

    def save_itemsets(self, dataset_id: int, table: FrequentItemsetTable) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM itemsets WHERE dataset_id = ?", (dataset_id,))
            conn.executemany(
                "INSERT INTO itemsets(dataset_id, items_json, size, support_count, support)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        dataset_id,
                        json.dumps(list(fi.items)),
                        len(fi.items),
                        fi.support_count,
                        fi.support,
                    )
                    for fi in table.all()
                ],
            )

    def load_itemsets(self, dataset_id: int) -> FrequentItemsetTable:
        n_row = self._conn.execute(
            "SELECT n_transactions FROM datasets WHERE id = ?", (dataset_id,)
        ).fetchone()
        if n_row is None:
            raise KeyError(f"dataset id {dataset_id} not found")
        rows = self._conn.execute(
            "SELECT items_json, support_count FROM itemsets WHERE dataset_id = ?",
            (dataset_id,),
        ).fetchall()
        counts: Dict[Itemset, int] = {}
        for row in rows:
            items = tuple(json.loads(row["items_json"]))
            counts[items] = row["support_count"]
        return FrequentItemsetTable.from_counts(counts, int(n_row["n_transactions"]))

    def save_audit_run(
        self, dataset_id: int, request_id: str, params: Dict[str, Any], summary: Dict[str, Any]
    ) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO audit_runs(dataset_id, request_id, params_json, summary_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    dataset_id,
                    request_id,
                    json.dumps(params, default=str),
                    json.dumps(summary, default=str),
                    time.time(),
                ),
            )
            return int(cur.lastrowid)

    def list_datasets(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, name, n_transactions, created_at FROM datasets ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
