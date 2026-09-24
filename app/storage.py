"""SQLite persistence for uploaded DBC documents and a decode audit log.

The database is deliberately tiny: DBC text is the source of truth and is
re-parsed into memory on every use, while two tables support listing and
auditing.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from .dbc import parse_dbc
from .errors import NotFoundError
from .versioning import dbc_version

SCHEMA = """
CREATE TABLE IF NOT EXISTS dbc_documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    version     TEXT NOT NULL,
    content     TEXT NOT NULL,
    content_sha TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS decode_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dbc_name     TEXT NOT NULL,
    frame_id     INTEGER NOT NULL,
    dlc          INTEGER NOT NULL,
    data_hex     TEXT NOT NULL,
    message_name TEXT,
    ok           INTEGER NOT NULL,
    error        TEXT,
    created_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decode_log_name ON decode_log(dbc_name);
"""


@dataclass(frozen=True)
class StoredDBC:
    name: str
    version: str
    content: str
    content_sha: str
    created_at: float


class Store:
    """Thread-safe SQLite wrapper. A single lock serialises writes; the
    FastAPI service is low-throughput and every query is milliseconds."""

    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._cache: dict[str, tuple[str, object]] = {}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _cursor(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()

    # -- DBC documents ------------------------------------------------------- #

    def add_dbc(
        self,
        name: str,
        content: str,
        content_sha: str,
        *,
        allow_replace: bool = False,
    ) -> StoredDBC:
        """Validate and persist a DBC document.

        Raises :class:`DBCParseError`/:class:`DBCValidationError` for invalid
        content (nothing is written) and ``ValueError`` if the name already
        exists and replacement was not requested.
        """
        parsed = parse_dbc(content)  # validate before touching the DB
        version = dbc_version(parsed)
        now = time.time()

        with self._cursor() as cur:
            existing = cur.execute(
                "SELECT name FROM dbc_documents WHERE name = ?", (name,)
            ).fetchone()
            if existing is not None and not allow_replace:
                raise ValueError(f"DBC '{name}' already exists")
            if existing is not None:
                cur.execute(
                    "UPDATE dbc_documents SET version=?, content=?, "
                    "content_sha=?, created_at=? WHERE name=?",
                    (version, content, content_sha, now, name),
                )
            else:
                cur.execute(
                    "INSERT INTO dbc_documents (name, version, content, "
                    "content_sha, created_at) VALUES (?, ?, ?, ?, ?)",
                    (name, version, content, content_sha, now),
                )
        self._cache.pop(name, None)
        return StoredDBC(name, version, content, content_sha, now)

    def list_dbc(self) -> list[StoredDBC]:
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT name, version, content, content_sha, created_at "
                "FROM dbc_documents ORDER BY name"
            ).fetchall()
        return [StoredDBC(**dict(r)) for r in rows]

    def get_dbc(self, name: str) -> StoredDBC:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT name, version, content, content_sha, created_at "
                "FROM dbc_documents WHERE name = ?",
                (name,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"DBC '{name}' not found")
        return StoredDBC(**dict(row))

    def delete_dbc(self, name: str) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM dbc_documents WHERE name = ?", (name,))
        self._cache.pop(name, None)

    def get_parsed(self, name: str):
        """Return a parsed, cached :class:`~app.dbc.Database` for ``name``."""
        stored = self.get_dbc(name)
        cached = self._cache.get(name)
        if cached is not None and cached[0] == stored.content_sha:
            return stored, cached[1]
        parsed = parse_dbc(stored.content)
        self._cache[name] = (stored.content_sha, parsed)
        return stored, parsed

    # -- decode audit log ---------------------------------------------------- #

    def log_decode(
        self,
        dbc_name: str,
        frame_id: int,
        dlc: int,
        data_hex: str,
        message_name: str | None,
        ok: bool,
        error: str | None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO decode_log (dbc_name, frame_id, dlc, data_hex, "
                "message_name, ok, error, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    dbc_name,
                    frame_id,
                    dlc,
                    data_hex,
                    message_name,
                    1 if ok else 0,
                    error,
                    time.time(),
                ),
            )

    def recent_log(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            return list(
                cur.execute(
                    "SELECT dbc_name, frame_id, dlc, data_hex, message_name, "
                    "ok, error, created_at FROM decode_log "
                    "ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            )
