"""SQLite-backed storage for block headers, indexed events and balances.

Design notes
------------
* Every fetched block is retained (``blocks``), including orphaned blocks.
  ``canonical`` flags whether it currently belongs to the canonical chain.
* Events are keyed by ``(block_hash, tx_hash, log_index)`` rather than
  ``(tx_hash, log_index)``: the same transaction can be mined into two
  competing blocks (Anvil forks preserve the raw tx), so the block hash is
  what disambiguates the two rows.
* The balances table is the materialized result of folding canonical events.
  ``ingest_block`` applies forward effects in log order; ``detach_block``
  applies inverse effects in reverse log order. Both happen inside a single
  transaction with the header/event rows, so a crash can never leave state
  that disagrees with the indexed event set.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .effects import DecodedEvent, apply_forward, apply_inverse

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blocks (
    number      INTEGER NOT NULL,
    hash        TEXT PRIMARY KEY,
    parent_hash TEXT NOT NULL,
    log_count   INTEGER NOT NULL DEFAULT 0,
    canonical   INTEGER NOT NULL DEFAULT 1,
    seen_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocks_number_canonical
    ON blocks(number, canonical);

CREATE TABLE IF NOT EXISTS events (
    block_hash  TEXT NOT NULL,
    tx_hash     TEXT NOT NULL,
    log_index   INTEGER NOT NULL,
    block_number INTEGER NOT NULL,
    name        TEXT NOT NULL,
    account     TEXT NOT NULL,
    to_account  TEXT,
    amount      TEXT NOT NULL,
    tag         TEXT NOT NULL,
    PRIMARY KEY (block_hash, tx_hash, log_index),
    FOREIGN KEY (block_hash) REFERENCES blocks(hash)
);
CREATE INDEX IF NOT EXISTS idx_events_block ON events(block_hash);
CREATE INDEX IF NOT EXISTS idx_events_account ON events(account);

CREATE TABLE IF NOT EXISTS balances (
    account TEXT PRIMARY KEY,
    amount  TEXT NOT NULL
);
"""

META_TIP_NUMBER = "tip_number"
META_TIP_HASH = "tip_hash"


class IndexStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()

    # ---------------------------------------------------------------- meta

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: str | int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            self._conn.commit()

    def tip(self) -> tuple[int, str] | None:
        """Stored canonical tip ``(number, hash)``."""
        with self._lock:
            n = self.get_meta(META_TIP_NUMBER)
            h = self.get_meta(META_TIP_HASH)
            return (int(n), h) if n is not None and h is not None else None

    def set_tip(self, number: int, block_hash: str) -> None:
        self.set_meta(META_TIP_NUMBER, number)
        self.set_meta(META_TIP_HASH, block_hash)

    # ------------------------------------------------------------- blocks

    def has_block(self, block_hash: str) -> bool:
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM blocks WHERE hash = ?", (block_hash,)
            ).fetchone() is not None

    def canonical_hash_at(self, number: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT hash FROM blocks WHERE number = ? AND canonical = 1", (number,)
            ).fetchone()
            return row["hash"] if row else None

    def get_block(self, block_hash: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM blocks WHERE hash = ?", (block_hash,)
            ).fetchone()

    def canonical_head_number(self) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(number) AS n FROM blocks WHERE canonical = 1"
            ).fetchone()
            return row["n"]

    # --------------------------------------------------- ingest / detach

    def ingest_block(
        self,
        number: int,
        block_hash: str,
        parent_hash: str,
        events: Iterable[DecodedEvent],
    ) -> None:
        """Attach one block to the canonical chain and apply event effects."""
        events = list(events)
        with self._lock:
            try:
                now = datetime.now(timezone.utc).isoformat()
                self._conn.execute(
                    "INSERT INTO blocks(number, hash, parent_hash, log_count, canonical, seen_at)"
                    " VALUES(?, ?, ?, ?, 1, ?)",
                    (number, block_hash, parent_hash, len(events), now),
                )
                balances = self._load_balances()
                for ev in events:
                    self._conn.execute(
                        "INSERT INTO events(block_hash, tx_hash, log_index, block_number,"
                        " name, account, to_account, amount, tag)"
                        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            block_hash,
                            ev["tx_hash"],
                            ev["log_index"],
                            number,
                            ev["name"],
                            ev["account"],
                            ev.get("to_account"),
                            str(ev["amount"]),
                            str(ev["tag"]),
                        ),
                    )
                    apply_forward(balances, ev)
                self._save_balances(balances)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def readopt_block(
        self,
        number: int,
        block_hash: str,
        parent_hash: str,
        events: Iterable[DecodedEvent],
    ) -> None:
        """Re-adopt a previously detached block that is canonical again.

        The header row survived as an orphan. Event rows were deleted, so they
        are re-inserted (RPC refetch is authoritative) and forward effects
        reapplied in log order.
        """
        events = list(events)
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT canonical, parent_hash FROM blocks WHERE hash = ?",
                    (block_hash,),
                ).fetchone()
                if cur is None:
                    raise ValueError(f"block {block_hash} not previously seen")
                if cur["canonical"] == 1:
                    raise ValueError(f"block {block_hash} already canonical")
                self._conn.execute(
                    "UPDATE blocks SET canonical = 1, parent_hash = ?, log_count = ?"
                    " WHERE hash = ?",
                    (parent_hash, len(events), block_hash),
                )
                balances = self._load_balances()
                for ev in sorted(events, key=lambda e: e["log_index"]):
                    self._conn.execute(
                        "INSERT INTO events(block_hash, tx_hash, log_index, block_number,"
                        " name, account, to_account, amount, tag)"
                        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            block_hash,
                            ev["tx_hash"],
                            ev["log_index"],
                            number,
                            ev["name"],
                            ev["account"],
                            ev.get("to_account"),
                            str(ev["amount"]),
                            str(ev["tag"]),
                        ),
                    )
                    apply_forward(balances, ev)
                self._save_balances(balances)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def detach_block(self, block_hash: str) -> list[DecodedEvent]:
        """Detach a canonical tip block, applying inverse effects.

        Returns the detached events in reverse log order.
        """
        with self._lock:
            try:
                block = self._conn.execute(
                    "SELECT * FROM blocks WHERE hash = ? AND canonical = 1",
                    (block_hash,),
                ).fetchone()
                if block is None:
                    raise ValueError(f"block {block_hash} is not a canonical block")
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE block_hash = ? ORDER BY log_index DESC",
                    (block_hash,),
                ).fetchall()
                balances = self._load_balances()
                detached: list[DecodedEvent] = []
                for row in rows:
                    ev = self._row_to_event(row)
                    apply_inverse(balances, ev)
                    detached.append(ev)
                self._save_balances(balances)
                self._conn.execute(
                    "DELETE FROM events WHERE block_hash = ?", (block_hash,)
                )
                self._conn.execute(
                    "UPDATE blocks SET canonical = 0 WHERE hash = ?", (block_hash,)
                )
                self._conn.commit()
                return detached
            except Exception:
                self._conn.rollback()
                raise

    # ------------------------------------------------------------- reads

    def list_events(self, canonical_only: bool = True) -> list[dict]:
        with self._lock:
            sql = (
                "SELECT e.*, b.canonical FROM events e"
                " JOIN blocks b ON b.hash = e.block_hash"
            )
            if canonical_only:
                sql += " WHERE b.canonical = 1"
            sql += " ORDER BY e.block_number ASC, e.log_index ASC"
            return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def list_blocks(self, canonical_only: bool = True) -> list[dict]:
        with self._lock:
            sql = (
                "SELECT number, hash, parent_hash, log_count, canonical, seen_at"
                " FROM blocks"
            )
            if canonical_only:
                sql += " WHERE canonical = 1"
            sql += " ORDER BY number"
            return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def list_orphan_blocks(self) -> list[dict]:
        with self._lock:
            return [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM blocks WHERE canonical = 0 ORDER BY number"
                ).fetchall()
            ]

    def list_balances(self) -> dict[str, int]:
        with self._lock:
            return {
                r["account"]: int(r["amount"])
                for r in self._conn.execute(
                    "SELECT account, amount FROM balances ORDER BY account"
                ).fetchall()
            }

    def block_counts(self) -> dict[str, int]:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) c FROM blocks").fetchone()["c"]
            orphan = self._conn.execute(
                "SELECT COUNT(*) c FROM blocks WHERE canonical = 0"
            ).fetchone()["c"]
            events = self._conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
            return {
                "blocks_total": total,
                "blocks_canonical": total - orphan,
                "blocks_orphan": orphan,
                "events_rows": events,
            }

    # ------------------------------------------------------------ helpers

    def _load_balances(self) -> dict[str, int]:
        return {
            r["account"]: int(r["amount"])
            for r in self._conn.execute("SELECT account, amount FROM balances").fetchall()
        }

    def _save_balances(self, balances: dict[str, int]) -> None:
        for account, amount in balances.items():
            self._conn.execute(
                "INSERT INTO balances(account, amount) VALUES(?, ?)"
                " ON CONFLICT(account) DO UPDATE SET amount = excluded.amount",
                (account, str(amount)),
            )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> DecodedEvent:
        return {
            "name": row["name"],
            "account": row["account"],
            "to_account": row["to_account"],
            "amount": int(row["amount"]),
            "tag": int(row["tag"]),
            "tx_hash": row["tx_hash"],
            "log_index": row["log_index"],
            "block_hash": row["block_hash"],
            "block_number": row["block_number"],
        }
