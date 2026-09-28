"""SQLite evidence store.

Persists, per session:
- the session row (budget snapshot, creation time),
- an append-only operation log (assumptions, retractions, premises, rules)
  sufficient to rebuild the in-memory engine deterministically,
- diagnostic records explaining each accept/reject/incomplete decision.

Only ids, counts and (optionally redacted) symbol names are stored in
diagnostics -- never free-form payloads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    budget_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    request_id  TEXT NOT NULL,
    op          TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS diagnostics (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT,
    request_id  TEXT NOT NULL,
    level       TEXT NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class EvidenceStore:
    """Thread-safe thin wrapper over a SQLite database file."""

    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    # sessions + operation log
    # ------------------------------------------------------------------ #

    def create_session(self, session_id: str, budget: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions(session_id, created_at, budget_json)"
                " VALUES (?, ?, ?)",
                (session_id, _utcnow(), json.dumps(budget)),
            )
            self._conn.commit()

    def session_exists(self, session_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return row is not None

    def list_sessions(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id FROM sessions ORDER BY created_at"
            ).fetchall()
        return [row["session_id"] for row in rows]

    def append_operation(
        self, session_id: str, request_id: str, op: str, payload: dict
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO operations(session_id, request_id, op, payload,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, request_id, op, json.dumps(payload), _utcnow()),
            )
            self._conn.commit()

    def load_operations(self, session_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT op, payload FROM operations WHERE session_id = ?"
                " ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [{"op": row["op"], "payload": json.loads(row["payload"])} for row in rows]

    # ------------------------------------------------------------------ #
    # diagnostics
    # ------------------------------------------------------------------ #

    def append_diagnostic(
        self,
        request_id: str,
        event: str,
        detail: dict,
        session_id: str | None = None,
        level: str = "info",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO diagnostics(session_id, request_id, level, event,"
                " detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, request_id, level, event, json.dumps(detail), _utcnow()),
            )
            self._conn.commit()

    def load_diagnostics(self, session_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT request_id, level, event, detail, created_at"
                " FROM diagnostics WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [
            {
                "request_id": row["request_id"],
                "level": row["level"],
                "event": row["event"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
