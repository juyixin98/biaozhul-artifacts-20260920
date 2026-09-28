"""SQLite audit trail.

Every analysis call is persisted keyed by its caller-supplied ``request_id`` so
results and logs are traceable to a request identity and processing location.
Local file only; no network.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id   TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    status       TEXT NOT NULL,
    service      TEXT NOT NULL,
    version      TEXT NOT NULL,
    host         TEXT NOT NULL,
    created_at   REAL NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT
);
"""


class AuditStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        directory = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(directory, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with _LOCK, self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.commit()

    def record_run(
        self,
        request_id: str,
        endpoint: str,
        status: str,
        service: str,
        version: str,
        host: str,
        request_obj: Any,
        response_obj: Optional[Any],
    ) -> int:
        with _LOCK, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO analysis_runs "
                "(request_id, endpoint, status, service, version, host, "
                "created_at, request_json, response_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    endpoint,
                    status,
                    service,
                    version,
                    host,
                    time.time(),
                    json.dumps(request_obj, ensure_ascii=False, sort_keys=True),
                    json.dumps(response_obj, ensure_ascii=False, sort_keys=True)
                    if response_obj is not None
                    else None,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def fetch_run(self, run_id: int) -> Optional[dict]:
        with _LOCK, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM analysis_runs WHERE id = ?", (run_id,)
            ).fetchone()
            return dict(row) if row else None

    def find_by_request_id(self, request_id: str) -> list[dict]:
        with _LOCK, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM analysis_runs WHERE request_id = ? ORDER BY id",
                (request_id,),
            ).fetchall()
            return [dict(r) for r in rows]
