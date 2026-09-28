"""SQLite storage layer.

Schema (all in one file, created idempotently):

  corpora(id, name, n_transactions, created_at)
  transactions(corpus_id, transaction_id, n_items_raw, n_items_dedup)
  transaction_items(corpus_id, transaction_id, item)      -- deduplicated
  itemsets(corpus_id, items_json, size, support_count, support, min_support)
  rules(corpus_id, rule_id, antecedent_json, consequent_json,
        support, confidence, lift, leverage, warnings_json)

The store is deliberately thin: it persists records and answers support
queries; all mining logic lives in ``app.mining``.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS corpora (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    n_transactions INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    corpus_id INTEGER NOT NULL REFERENCES corpora(id),
    transaction_id TEXT NOT NULL,
    n_items_raw INTEGER NOT NULL,
    n_items_dedup INTEGER NOT NULL,
    PRIMARY KEY (corpus_id, transaction_id)
);
CREATE TABLE IF NOT EXISTS transaction_items (
    corpus_id INTEGER NOT NULL,
    transaction_id TEXT NOT NULL,
    item TEXT NOT NULL,
    PRIMARY KEY (corpus_id, transaction_id, item)
);
CREATE INDEX IF NOT EXISTS idx_items_item ON transaction_items(corpus_id, item);
CREATE TABLE IF NOT EXISTS itemsets (
    corpus_id INTEGER NOT NULL REFERENCES corpora(id),
    items_json TEXT NOT NULL,
    size INTEGER NOT NULL,
    support_count INTEGER NOT NULL,
    support REAL NOT NULL,
    min_support REAL NOT NULL,
    PRIMARY KEY (corpus_id, items_json)
);
CREATE TABLE IF NOT EXISTS rules (
    corpus_id INTEGER NOT NULL REFERENCES corpora(id),
    rule_id TEXT NOT NULL,
    antecedent_json TEXT NOT NULL,
    consequent_json TEXT NOT NULL,
    support REAL,
    confidence REAL,
    lift REAL,
    leverage REAL,
    warnings_json TEXT NOT NULL,
    PRIMARY KEY (corpus_id, rule_id)
);
"""


class Store:
    """Thin SQLite-backed repository for corpora, itemsets and rules."""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- corpora -----------------------------------------------------------

    def create_corpus(self, name: str, n_transactions: int) -> int:
        cur = self._conn.execute(
            "INSERT INTO corpora(name, n_transactions, created_at) VALUES (?, ?, ?)",
            (name, n_transactions, time.time()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def get_corpus(self, corpus_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM corpora WHERE id = ?", (corpus_id,)
        ).fetchone()

    # -- transactions ------------------------------------------------------

    def insert_transactions(
        self,
        corpus_id: int,
        rows: Iterable[tuple[str, int, int, list[str]]],
    ) -> None:
        """Insert ``(transaction_id, n_raw, n_dedup, items)`` rows."""
        for tid, n_raw, n_dedup, items in rows:
            self._conn.execute(
                "INSERT INTO transactions(corpus_id, transaction_id, n_items_raw, n_items_dedup)"
                " VALUES (?, ?, ?, ?)",
                (corpus_id, tid, n_raw, n_dedup),
            )
            self._conn.executemany(
                "INSERT INTO transaction_items(corpus_id, transaction_id, item) VALUES (?, ?, ?)",
                [(corpus_id, tid, item) for item in items],
            )
        self._conn.commit()

    def iter_transaction_itemsets(self, corpus_id: int) -> Iterator[frozenset[str]]:
        """Yield each transaction of a corpus as a deduplicated frozenset."""
        cur = self._conn.execute(
            "SELECT transaction_id, item FROM transaction_items WHERE corpus_id = ?"
            " ORDER BY transaction_id",
            (corpus_id,),
        )
        current_tid: str | None = None
        current: set[str] = set()
        for row in cur:
            if row["transaction_id"] != current_tid:
                if current_tid is not None:
                    yield frozenset(current)
                current_tid = row["transaction_id"]
                current = set()
            current.add(row["item"])
        if current_tid is not None:
            yield frozenset(current)

    def transaction_count(self, corpus_id: int) -> int:
        row = self._conn.execute(
            "SELECT n_transactions AS n FROM corpora WHERE id = ?", (corpus_id,)
        ).fetchone()
        return int(row["n"]) if row else 0

    def support_count(self, corpus_id: int, items: frozenset[str]) -> int:
        """Count transactions containing every item in ``items``."""
        if not items:
            return 0
        placeholders = ",".join("?" for _ in items)
        row = self._conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM (
                SELECT transaction_id FROM transaction_items
                WHERE corpus_id = ? AND item IN ({placeholders})
                GROUP BY transaction_id
                HAVING COUNT(DISTINCT item) = ?
            )
            """,
            (corpus_id, *sorted(items), len(items)),
        ).fetchone()
        return int(row["n"])

    # -- itemsets / rules --------------------------------------------------

    def replace_itemsets(
        self,
        corpus_id: int,
        itemsets: Iterable[tuple[frozenset[str], int, float]],
        min_support: float,
    ) -> None:
        self._conn.execute("DELETE FROM itemsets WHERE corpus_id = ?", (corpus_id,))
        self._conn.executemany(
            "INSERT INTO itemsets(corpus_id, items_json, size, support_count, support, min_support)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (corpus_id, json.dumps(sorted(items)), len(items), count, support, min_support)
                for items, count, support in itemsets
            ],
        )
        self._conn.commit()

    def get_itemsets(self, corpus_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM itemsets WHERE corpus_id = ? ORDER BY size, items_json",
                (corpus_id,),
            )
        )

    def replace_rules(self, corpus_id: int, rules: Iterable[dict]) -> None:
        self._conn.execute("DELETE FROM rules WHERE corpus_id = ?", (corpus_id,))
        self._conn.executemany(
            "INSERT INTO rules(corpus_id, rule_id, antecedent_json, consequent_json,"
            " support, confidence, lift, leverage, warnings_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    corpus_id,
                    r["rule_id"],
                    json.dumps(sorted(r["antecedent"])),
                    json.dumps(sorted(r["consequent"])),
                    r["support"],
                    r["confidence"],
                    r["lift"],
                    r["leverage"],
                    json.dumps(r["warnings"]),
                )
                for r in rules
            ],
        )
        self._conn.commit()

    def get_rules(self, corpus_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute("SELECT * FROM rules WHERE corpus_id = ?", (corpus_id,))
        )
