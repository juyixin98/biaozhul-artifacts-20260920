"""SQLite persistence layer.

The store is the single source of truth. Every business decision is taken
inside a short ``with store.transaction()`` block so a crash leaves either the
old state or the new state, never a half-written vote.

Schema overview
---------------
``epochs``        one row per epoch; the validator set is frozen at creation
``validators``    validator public keys + integer weight, scoped to an epoch
``votes``         every accepted signature (even post-equivocation evidence)
``exclusions``    validators whose weight is excluded for an epoch, with proof
``checkpoints``   at most one finalized value per (epoch, height)
``alarms``        finality-conflict alarms; an open alarm freezes its epoch
``meta``          small key/value table (schema version, chain id)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1


class Store:
    """Thin, explicit wrapper around a SQLite database.

    A single connection is shared behind a re-entrant lock: writes are
    serialized and each transaction is committed or rolled back atomically.
    WAL mode lets status reads proceed while a vote is being written.
    """

    def __init__(self, db_path: str, *, reset: bool = False) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        if reset and db_path != ":memory:":
            Path(db_path).unlink(missing_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create_schema()

    # ------------------------------------------------------------------ basics

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS epochs (
                    epoch_id   INTEGER PRIMARY KEY,
                    height     INTEGER NOT NULL,
                    total_weight INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    frozen     INTEGER NOT NULL DEFAULT 0,
                    finalized_value TEXT
                );

                CREATE TABLE IF NOT EXISTS validators (
                    epoch_id    INTEGER NOT NULL REFERENCES epochs(epoch_id),
                    validator_id TEXT NOT NULL,
                    public_key  TEXT NOT NULL,
                    weight      INTEGER NOT NULL,
                    PRIMARY KEY (epoch_id, validator_id)
                );

                CREATE TABLE IF NOT EXISTS votes (
                    epoch_id    INTEGER NOT NULL REFERENCES epochs(epoch_id),
                    height      INTEGER NOT NULL,
                    validator_id TEXT NOT NULL,
                    value       TEXT NOT NULL,
                    signature   TEXT NOT NULL,
                    seq         INTEGER NOT NULL,
                    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    PRIMARY KEY (epoch_id, validator_id, seq)
                );

                -- An equivocation is the first differing value a validator signs
                -- for a given (epoch, height).
                CREATE TABLE IF NOT EXISTS exclusions (
                    epoch_id      INTEGER NOT NULL REFERENCES epochs(epoch_id),
                    validator_id  TEXT NOT NULL,
                    height        INTEGER NOT NULL,
                    reason        TEXT NOT NULL,
                    vote_a_seq    INTEGER NOT NULL,
                    vote_b_seq    INTEGER NOT NULL,
                    detected_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    PRIMARY KEY (epoch_id, validator_id)
                );

                -- Only one finalized value may ever exist for a position.
                CREATE TABLE IF NOT EXISTS checkpoints (
                    epoch_id  INTEGER NOT NULL REFERENCES epochs(epoch_id),
                    height    INTEGER NOT NULL,
                    value     TEXT NOT NULL,
                    power     INTEGER NOT NULL,
                    total_weight INTEGER NOT NULL,
                    finalized_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    PRIMARY KEY (epoch_id, height)
                );

                CREATE TABLE IF NOT EXISTS alarms (
                    alarm_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind        TEXT NOT NULL,
                    epoch_id    INTEGER NOT NULL,
                    height      INTEGER NOT NULL,
                    detail      TEXT NOT NULL,
                    resolved    INTEGER NOT NULL DEFAULT 0,
                    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                """
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize writers and commit on success / roll back on error."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # --------------------------------------------------------------- accessors

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchone()

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    # ------------------------------------------------------------------ epochs

    def create_epoch(
        self, conn: sqlite3.Connection, epoch_id: int, height: int, total_weight: int
    ) -> None:
        conn.execute(
            "INSERT INTO epochs(epoch_id, height, total_weight) VALUES (?, ?, ?)",
            (epoch_id, height, total_weight),
        )

    def add_validator(
        self,
        conn: sqlite3.Connection,
        epoch_id: int,
        validator_id: str,
        public_key: str,
        weight: int,
    ) -> None:
        conn.execute(
            "INSERT INTO validators(epoch_id, validator_id, public_key, weight)"
            " VALUES (?, ?, ?, ?)",
            (epoch_id, validator_id, public_key, weight),
        )

    def get_epoch(self, epoch_id: int) -> sqlite3.Row | None:
        return self.fetchone("SELECT * FROM epochs WHERE epoch_id = ?", (epoch_id,))

    def list_epochs(self) -> list[sqlite3.Row]:
        return self.fetchall("SELECT * FROM epochs ORDER BY epoch_id")

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        row = self.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    # ------------------------------------------------------------------- votes

    def next_vote_seq(self, conn: sqlite3.Connection, epoch_id: int) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM votes WHERE epoch_id = ?",
            (epoch_id,),
        ).fetchone()
        return int(row["n"])

    def insert_vote(
        self,
        conn: sqlite3.Connection,
        epoch_id: int,
        height: int,
        validator_id: str,
        value: str,
        signature: str,
        seq: int,
    ) -> None:
        conn.execute(
            "INSERT INTO votes(epoch_id, height, validator_id, value, signature, seq)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (epoch_id, height, validator_id, value, signature, seq),
        )

    def votes_for(self, epoch_id: int, height: int) -> list[sqlite3.Row]:
        return self.fetchall(
            "SELECT * FROM votes WHERE epoch_id = ? AND height = ? ORDER BY seq",
            (epoch_id, height),
        )

    def all_votes(self, epoch_id: int) -> list[sqlite3.Row]:
        return self.fetchall(
            "SELECT * FROM votes WHERE epoch_id = ? ORDER BY height, seq", (epoch_id,)
        )

    # -------------------------------------------------------------- exclusions

    def exclude_validator(
        self,
        conn: sqlite3.Connection,
        epoch_id: int,
        validator_id: str,
        height: int,
        reason: str,
        vote_a_seq: int,
        vote_b_seq: int,
    ) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO exclusions"
            "(epoch_id, validator_id, height, reason, vote_a_seq, vote_b_seq)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (epoch_id, validator_id, height, reason, vote_a_seq, vote_b_seq),
        )

    def excluded_validators(self, epoch_id: int) -> set[str]:
        rows = self.fetchall(
            "SELECT validator_id FROM exclusions WHERE epoch_id = ?", (epoch_id,)
        )
        return {r["validator_id"] for r in rows}

    def exclusions_for(self, epoch_id: int) -> list[sqlite3.Row]:
        return self.fetchall(
            "SELECT * FROM exclusions WHERE epoch_id = ? ORDER BY validator_id",
            (epoch_id,),
        )

    # ----------------------------------------------------- checkpoints/alarms

    def finalize(
        self,
        conn: sqlite3.Connection,
        epoch_id: int,
        height: int,
        value: str,
        power: int,
        total_weight: int,
    ) -> None:
        conn.execute(
            "INSERT INTO checkpoints(epoch_id, height, value, power, total_weight)"
            " VALUES (?, ?, ?, ?, ?)",
            (epoch_id, height, value, power, total_weight),
        )
        conn.execute(
            "UPDATE epochs SET finalized_value = ? WHERE epoch_id = ?",
            (value, epoch_id),
        )

    def get_checkpoint(self, epoch_id: int, height: int) -> sqlite3.Row | None:
        return self.fetchone(
            "SELECT * FROM checkpoints WHERE epoch_id = ? AND height = ?",
            (epoch_id, height),
        )

    def raise_alarm(
        self,
        conn: sqlite3.Connection,
        kind: str,
        epoch_id: int,
        height: int,
        detail: dict[str, Any] | str,
    ) -> int:
        cur = conn.execute(
            "INSERT INTO alarms(kind, epoch_id, height, detail) VALUES (?, ?, ?, ?)",
            (kind, epoch_id, height, json.dumps(detail) if isinstance(detail, dict) else detail),
        )
        alarm_id = int(cur.lastrowid)
        conn.execute("UPDATE epochs SET frozen = 1 WHERE epoch_id = ?", (epoch_id,))
        return alarm_id

    def freeze_epoch(self, conn: sqlite3.Connection, epoch_id: int) -> None:
        conn.execute("UPDATE epochs SET frozen = 1 WHERE epoch_id = ?", (epoch_id,))

    def open_alarms(self, epoch_id: int | None = None) -> list[sqlite3.Row]:
        if epoch_id is None:
            return self.fetchall("SELECT * FROM alarms WHERE resolved = 0 ORDER BY alarm_id")
        return self.fetchall(
            "SELECT * FROM alarms WHERE resolved = 0 AND epoch_id = ? ORDER BY alarm_id",
            (epoch_id,),
        )

    def alarms(self, epoch_id: int | None = None) -> list[sqlite3.Row]:
        if epoch_id is None:
            return self.fetchall("SELECT * FROM alarms ORDER BY alarm_id")
        return self.fetchall(
            "SELECT * FROM alarms WHERE epoch_id = ? ORDER BY alarm_id", (epoch_id,)
        )

    def validators_for(self, epoch_id: int) -> list[sqlite3.Row]:
        return self.fetchall(
            "SELECT * FROM validators WHERE epoch_id = ? ORDER BY validator_id",
            (epoch_id,),
        )
