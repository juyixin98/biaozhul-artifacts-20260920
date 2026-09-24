"""SQLite persistence for the indexer.

Three kinds of data live here:

1. **Block chain** (``blocks``): one row per *indexed* block, keyed by both
   number and hash, recording ``parent_hash``. This is the hash-linked chain
   the indexer walks to detect forks.
2. **Event log** (``events``): every decoded Vault event positioned by
   ``(block_hash, tx_hash, log_index)`` -- the tuple that uniquely identifies a
   log even when the *same transaction hash* is later mined in a different
   block (Anvil reorgs reproduce exactly this situation).
3. **Materialized business state** (``balances`` / ``totals``): the reversible
   projection derived from the event log.

Only canonical (currently best) chain blocks/events remain in the tables:
rollback deletes the forked-out rows after inverting their state effects, so
uncle events never linger in query results.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks (
    block_number INTEGER PRIMARY KEY,
    block_hash   TEXT NOT NULL UNIQUE,
    parent_hash  TEXT NOT NULL,
    UNIQUE(block_number, block_hash)
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    block_number  INTEGER NOT NULL,
    block_hash    TEXT NOT NULL,
    tx_hash       TEXT NOT NULL,
    tx_index      INTEGER NOT NULL,
    log_index     INTEGER NOT NULL,
    event_name    TEXT NOT NULL,
    who           TEXT NOT NULL,
    amount        TEXT NOT NULL,
    UNIQUE(block_hash, tx_hash, log_index),
    FOREIGN KEY(block_number, block_hash) REFERENCES blocks(block_number, block_hash)
);
CREATE INDEX IF NOT EXISTS events_block_idx ON events(block_number, tx_index, log_index);
CREATE INDEX IF NOT EXISTS events_who_idx   ON events(who, block_number);

CREATE TABLE IF NOT EXISTS balances (
    who     TEXT PRIMARY KEY,
    balance TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS totals (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

TIP_NUMBER = "tip_block_number"
TIP_HASH = "tip_block_hash"


@dataclass(frozen=True)
class BlockRow:
    number: int
    hash: str
    parent_hash: str


@dataclass(frozen=True)
class EventRow:
    block_number: int
    block_hash: str
    tx_hash: str
    tx_index: int
    log_index: int
    event_name: str
    who: str
    amount: int


class Storage:
    """Thread-safe wrapper around a single SQLite connection."""

    def __init__(self, path: str = ":memory:"):
        self._lock = threading.RLock()
        # isolation_level=None: we manage BEGIN/COMMIT ourselves (the sync
        # methods need one explicit multi-statement transaction).
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA defer_foreign_keys=ON")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- transactions -----------------------------------------------------
    def __enter__(self) -> "Storage":
        self._lock.acquire()
        self._conn.execute("BEGIN")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._lock.release()

    # ---- meta -------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    # ---- block chain ------------------------------------------------------
    def tip(self) -> BlockRow | None:
        with self._lock:
            num = self.get_meta(TIP_NUMBER)
            if num is None:
                return None
            return self.get_block(int(num))

    def get_block(self, number: int) -> BlockRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT block_number, block_hash, parent_hash FROM blocks WHERE block_number=?",
                (number,),
            ).fetchone()
            if not row:
                return None
            return BlockRow(row["block_number"], row["block_hash"], row["parent_hash"])

    def get_block_by_hash(self, block_hash: str) -> BlockRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT block_number, block_hash, parent_hash FROM blocks WHERE block_hash=?",
                (block_hash,),
            ).fetchone()
            if not row:
                return None
            return BlockRow(row["block_number"], row["block_hash"], row["parent_hash"])

    def insert_block(self, row: BlockRow) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO blocks(block_number, block_hash, parent_hash) VALUES(?, ?, ?)",
                (row.number, row.hash, row.parent_hash),
            )

    def delete_block(self, number: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM blocks WHERE block_number=?", (number,))

    def block_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) AS n FROM blocks").fetchone()["n"]

    # ---- events -----------------------------------------------------------
    def insert_event(self, ev: EventRow) -> bool:
        """Insert an event row. Returns False if it already exists (idempotent)."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO events(block_number, block_hash, tx_hash, tx_index, "
                "log_index, event_name, who, amount) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ev.block_number,
                    ev.block_hash,
                    ev.tx_hash,
                    ev.tx_index,
                    ev.log_index,
                    ev.event_name,
                    ev.who,
                    str(ev.amount),
                ),
            )
            return cur.rowcount > 0

    def events_on_block(self, number: int, reverse: bool = False) -> list[EventRow]:
        order = "DESC" if reverse else "ASC"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM events WHERE block_number=? "
                f"ORDER BY tx_index {order}, log_index {order}",
                (number,),
            ).fetchall()
            return [self._row_to_event(r) for r in rows]

    def delete_events_on_block(self, number: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM events WHERE block_number=?", (number,))

    def event_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]

    def list_events(self, limit: int = 100) -> list[EventRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY block_number DESC, tx_index DESC, log_index DESC "
                "LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._row_to_event(r) for r in rows]

    def events_for(self, who: str) -> list[EventRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE who=? ORDER BY block_number, tx_index, log_index",
                (who,),
            ).fetchall()
            return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(r: sqlite3.Row) -> EventRow:
        return EventRow(
            block_number=r["block_number"],
            block_hash=r["block_hash"],
            tx_hash=r["tx_hash"],
            tx_index=r["tx_index"],
            log_index=r["log_index"],
            event_name=r["event_name"],
            who=r["who"],
            amount=int(r["amount"]),
        )

    # ---- materialized state ----------------------------------------------
    def balance_add(self, who: str, delta: int) -> int:
        """Add a signed delta to an account balance; returns the new balance.

        Rows that return to zero are deleted, so a reorged-away account leaves
        no residue and a fresh canonical-chain replay compares byte-identical.
        """
        with self._lock:
            row = self._conn.execute("SELECT balance FROM balances WHERE who=?", (who,)).fetchone()
            current = int(row["balance"]) if row else 0
            new = current + delta
            if new < 0:
                raise ValueError(f"negative balance for {who}: {current} + {delta}")
            if new == 0:
                self._conn.execute("DELETE FROM balances WHERE who=?", (who,))
            else:
                self._conn.execute(
                    "INSERT INTO balances(who, balance) VALUES(?, ?) "
                    "ON CONFLICT(who) DO UPDATE SET balance=excluded.balance",
                    (who, str(new)),
                )
            return new

    def total_add(self, key: str, delta: int) -> int:
        with self._lock:
            row = self._conn.execute("SELECT value FROM totals WHERE key=?", (key,)).fetchone()
            current = int(row["value"]) if row else 0
            new = current + delta
            if new < 0:
                raise ValueError(f"negative total {key}: {current} + {delta}")
            if new == 0:
                self._conn.execute("DELETE FROM totals WHERE key=?", (key,))
            else:
                self._conn.execute(
                    "INSERT INTO totals(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(new)),
                )
            return new

    def get_balance(self, who: str) -> int:
        with self._lock:
            row = self._conn.execute("SELECT balance FROM balances WHERE who=?", (who,)).fetchone()
            return int(row["balance"]) if row else 0

    def all_balances(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT who, balance FROM balances").fetchall()
            return {r["who"]: int(r["balance"]) for r in rows}

    def get_total(self, key: str) -> int:
        with self._lock:
            row = self._conn.execute("SELECT value FROM totals WHERE key=?", (key,)).fetchone()
            return int(row["value"]) if row else 0

    def debug_rows(self) -> dict[str, Any]:
        """Snapshot used by tests and the /debug endpoint."""
        with self._lock:
            blocks = [
                {"number": r["block_number"], "hash": r["block_hash"], "parent": r["parent_hash"]}
                for r in self._conn.execute(
                    "SELECT * FROM blocks ORDER BY block_number"
                ).fetchall()
            ]
            return {
                "blocks": blocks,
                "event_count": self.event_count(),
                "balances": self.all_balances(),
                "totals": {
                    "deposited": self.get_total("deposited"),
                    "withdrawn": self.get_total("withdrawn"),
                },
            }
