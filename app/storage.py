"""SQLite persistence for DBC definitions and decoded frame history."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator

from . import crypto
from .dbc import Database

DEFAULT_DB_PATH = os.environ.get(
    "CANDECODE_DB", os.path.join(os.getcwd(), "candecode.db")
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS definitions (
    version_id  TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    source_hash TEXT NOT NULL,
    signature   TEXT NOT NULL,
    db_version  TEXT NOT NULL,
    nodes       TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    source      TEXT NOT NULL,
    canonical   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS frames (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id  TEXT NOT NULL REFERENCES definitions(version_id),
    frame_id    INTEGER NOT NULL,
    message_name TEXT NOT NULL,
    dlc         INTEGER NOT NULL,
    data_hex    TEXT NOT NULL,
    mux         INTEGER,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS decoded_signals (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    frame_pk  INTEGER NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    raw       INTEGER NOT NULL,
    value     REAL NOT NULL,
    unit      TEXT NOT NULL,
    mux_kind  TEXT NOT NULL,
    mux_value INTEGER
);

CREATE INDEX IF NOT EXISTS idx_frames_version ON frames(version_id);
CREATE INDEX IF NOT EXISTS idx_frames_frame_id ON frames(frame_id);
CREATE INDEX IF NOT EXISTS idx_signals_frame_pk ON decoded_signals(frame_pk);
"""


class Store:
    def __init__(self, path: str = DEFAULT_DB_PATH):
        self.path = path
        self._closed = False
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._conn.commit()
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def hmac_key(self) -> bytes:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            (crypto.HMAC_KEY_META_KEY,),
        ).fetchone()
        persisted = row["value"] if row else None
        key = crypto.resolve_key(persisted)
        if row is None:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?)",
                (crypto.HMAC_KEY_META_KEY, key.decode("ascii")),
            )
            self._conn.commit()
        return key

    def get_definition(self, version_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM definitions WHERE version_id = ?", (version_id,)
        ).fetchone()

    def latest_definition(self) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT version_id, source FROM definitions "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()

    def upsert_definition(
        self, database: Database, source: str
    ) -> tuple[str, str, bool]:
        """Idempotently store a definition.

        Returns ``(version_id, signature, created)`` where ``created`` is
        False when an identical definition already existed.
        """

        source_hash = crypto.sha256_text(source)
        version_id = crypto.fingerprint(database)
        canonical = crypto.canonical_json(database)
        key = self.hmac_key()
        signature = crypto.sign(version_id, key)

        existing = self.get_definition(version_id)
        if existing is not None:
            return version_id, existing["signature"], False

        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO definitions(
                    version_id, source_hash, signature, db_version, nodes,
                    message_count, source, canonical)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    source_hash,
                    signature,
                    database.version,
                    json.dumps(list(database.nodes)),
                    len(database.messages),
                    source,
                    canonical,
                ),
            )
        return version_id, signature, True

    def record_frame(
        self,
        version_id: str,
        frame_row: dict,
        signals: list[dict],
    ) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO frames(
                    version_id, frame_id, message_name, dlc, data_hex, mux)
                VALUES(:version_id, :frame_id, :message_name, :dlc,
                       :data_hex, :mux)
                """,
                {**frame_row, "version_id": version_id},
            )
            frame_pk = cur.lastrowid
            conn.executemany(
                """
                INSERT INTO decoded_signals(
                    frame_pk, name, raw, value, unit, mux_kind, mux_value)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        frame_pk,
                        s["name"],
                        s["raw"],
                        s["value"],
                        s["unit"],
                        s["mux_kind"],
                        s["mux_value"],
                    )
                    for s in signals
                ],
            )
        return frame_pk

    def list_frames(
        self,
        version_id: str | None = None,
        frame_id: int | None = None,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT f.*, d.signature FROM frames f "
            "JOIN definitions d ON d.version_id = f.version_id WHERE 1=1"
        )
        params: list = []
        if version_id is not None:
            sql += " AND f.version_id = ?"
            params.append(version_id)
        if frame_id is not None:
            sql += " AND f.frame_id = ?"
            params.append(frame_id)
        sql += " ORDER BY f.id DESC LIMIT ?"
        params.append(limit)
        return list(self._conn.execute(sql, params))

    def signals_for(self, frame_pk: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM decoded_signals WHERE frame_pk = ? "
                "ORDER BY id",
                (frame_pk,),
            )
        )
