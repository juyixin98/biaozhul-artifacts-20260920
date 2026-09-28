"""Evidence store: SQLite-backed persistence of requests and reasoning steps.

Each request is stored with its identity, engine version, processing
location (module/function), status, structured failure category, and the key
derivation steps that produced the answer -- so results are auditable after
the fact without re-running the request.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id      TEXT PRIMARY KEY,
    received_at     TEXT NOT NULL,
    engine_version  TEXT NOT NULL,
    endpoint        TEXT NOT NULL,
    status          TEXT NOT NULL,
    consistent      INTEGER,
    failure_categories TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    result_json     TEXT,
    error_code      TEXT,
    error_path      TEXT,
    error_message   TEXT,
    duration_ms     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS processing_steps (
    request_id  TEXT NOT NULL,
    step_index  INTEGER NOT NULL,
    stage       TEXT NOT NULL,
    location    TEXT NOT NULL,
    detail      TEXT NOT NULL,
    PRIMARY KEY (request_id, step_index),
    FOREIGN KEY (request_id) REFERENCES requests(request_id)
);

CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
"""


class EvidenceStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def save_request(
        self,
        *,
        request_id: str,
        received_at: str,
        engine_version: str,
        endpoint: str,
        status: str,
        consistent: bool | None,
        failure_categories: list[str],
        payload: dict[str, Any],
        result: dict[str, Any] | None,
        duration_ms: float,
        error_code: str | None = None,
        error_path: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO requests (
                    request_id, received_at, engine_version, endpoint, status,
                    consistent, failure_categories, payload_json, result_json,
                    error_code, error_path, error_message, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id, received_at, engine_version, endpoint, status,
                    None if consistent is None else int(consistent),
                    json.dumps(failure_categories, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False) if result else None,
                    error_code, error_path, error_message, duration_ms,
                ),
            )

    def save_steps(
        self, request_id: str, steps: list[dict[str, str]]
    ) -> None:
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO processing_steps
                    (request_id, step_index, stage, location, detail)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (request_id, i, s["stage"], s["location"], s["detail"])
                    for i, s in enumerate(steps)
                ],
            )

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                return None
            steps = conn.execute(
                """
                SELECT step_index, stage, location, detail
                FROM processing_steps WHERE request_id = ?
                ORDER BY step_index
                """,
                (request_id,),
            ).fetchall()
        record = dict(row)
        for key in ("failure_categories",):
            record[key] = json.loads(record[key])
        for key in ("payload_json", "result_json"):
            record[key] = json.loads(record[key]) if record[key] else None
        record["processing_steps"] = [dict(s) for s in steps]
        return record

    def count_requests(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
