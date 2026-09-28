"""SQLite evidence store.

Persists every planning request with its status, full expansion tree,
action sequence, failure and uncertainty evidence, and independent
verification report.  Read/write access is local SQLite only; no external
services are involved.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"
STORE_VERSION = "0.1.0"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    request_id      TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    domain_name     TEXT NOT NULL,
    domain_version  TEXT NOT NULL,
    problem_name    TEXT NOT NULL,
    engine_version  TEXT NOT NULL,
    store_version   TEXT NOT NULL,
    schema_version  TEXT NOT NULL,
    status          TEXT NOT NULL,
    duration_ms     REAL NOT NULL,
    domain_text     TEXT NOT NULL,
    problem_text    TEXT NOT NULL,
    result_json     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verifications (
    request_id      TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    ok              INTEGER NOT NULL,
    executable      INTEGER NOT NULL,
    hierarchy_ok    INTEGER NOT NULL,
    order_ok        INTEGER NOT NULL,
    report_json     TEXT NOT NULL,
    FOREIGN KEY(request_id) REFERENCES runs(request_id)
);

CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at);
"""


class StoreError(RuntimeError):
    pass


class EvidenceStore:
    """Thread-safe SQLite persistence for planning runs and verification."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "EvidenceStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # writes
    # ------------------------------------------------------------------ #

    def save_run(
        self,
        request_id: str,
        domain_text: str,
        problem_text: str,
        result: Any,
        domain_version: str,
    ) -> None:
        created = datetime.now(timezone.utc).isoformat()
        row = (
            request_id,
            created,
            result.domain_name,
            domain_version,
            result.problem_name,
            result.engine_version,
            STORE_VERSION,
            SCHEMA_VERSION,
            result.status,
            result.duration_ms,
            domain_text,
            problem_text,
            json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True),
        )
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO runs (
                        request_id, created_at, domain_name, domain_version,
                        problem_name, engine_version, store_version,
                        schema_version, status, duration_ms, domain_text,
                        problem_text, result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"run {request_id} already exists") from exc

    def save_verification(self, request_id: str, report: Any) -> None:
        created = datetime.now(timezone.utc).isoformat()
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM runs WHERE request_id = ?", (request_id,)
            ).fetchone()
            if exists is None:
                raise StoreError(f"unknown request_id {request_id}")
            self._conn.execute(
                """
                INSERT OR REPLACE INTO verifications
                    (request_id, created_at, ok, executable, hierarchy_ok,
                     order_ok, report_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    created,
                    int(report.ok),
                    int(report.executable),
                    int(report.hierarchy_consistent),
                    int(report.order_consistent),
                    json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True),
                ),
            )

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #

    def get_run(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE request_id = ?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["result"] = json.loads(data.pop("result_json"))
        return data

    def get_verification(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM verifications WHERE request_id = ?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["report"] = json.loads(data.pop("report_json"))
        return data

    def list_runs(
        self, limit: int = 50, status: str | None = None
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            if status is None:
                rows = self._conn.execute(
                    """
                    SELECT request_id, created_at, domain_name, problem_name,
                           status, engine_version, duration_ms
                    FROM runs ORDER BY created_at DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT request_id, created_at, domain_name, problem_name,
                           status, engine_version, duration_ms
                    FROM runs WHERE status = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (status, limit),
                ).fetchall()
        return [dict(r) for r in rows]
