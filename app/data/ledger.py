"""SQLite run ledger.

Persists one row per request (identity, endpoint, status, failure category,
core version, key steps) so a result can be explained after the fact without
scraping logs. This is a local dependency: a single file under ``data/``, no
server, no credentials.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id       TEXT NOT NULL,
    endpoint         TEXT NOT NULL,
    status           TEXT NOT NULL,
    failure_category TEXT,
    core_version     TEXT NOT NULL,
    point_estimate   REAL,
    standard_error   REAL,
    n_units          INTEGER,
    n_clusters       INTEGER,
    steps_json       TEXT,
    excluded_json    TEXT,
    diagnostics_json TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_runs_request_id ON runs(request_id);
"""


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(str(self.path))
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def record_run(self, record: dict[str, Any]) -> int:
        payload = dict(record)
        for key in ("steps", "excluded", "diagnostics"):
            if key in payload and not isinstance(payload[key], str):
                payload[key] = json.dumps(payload[key], ensure_ascii=False, default=str)
        with self._connect() as con:
            cur = con.execute(
                """
                INSERT INTO runs (
                    request_id, endpoint, status, failure_category, core_version,
                    point_estimate, standard_error, n_units, n_clusters,
                    steps_json, excluded_json, diagnostics_json
                ) VALUES (
                    :request_id, :endpoint, :status, :failure_category, :core_version,
                    :point_estimate, :standard_error, :n_units, :n_clusters,
                    :steps_json, :excluded_json, :diagnostics_json
                )
                """,
                {
                    "request_id": payload.get("request_id"),
                    "endpoint": payload.get("endpoint"),
                    "status": payload.get("status"),
                    "failure_category": payload.get("failure_category"),
                    "core_version": payload.get("core_version"),
                    "point_estimate": payload.get("point_estimate"),
                    "standard_error": payload.get("standard_error"),
                    "n_units": payload.get("n_units"),
                    "n_clusters": payload.get("n_clusters"),
                    "steps_json": payload.get("steps", "[]"),
                    "excluded_json": payload.get("excluded", "[]"),
                    "diagnostics_json": payload.get("diagnostics", "[]"),
                },
            )
            return int(cur.lastrowid)

    def fetch_run(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM runs WHERE request_id = ? ORDER BY id DESC LIMIT 1",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        out = dict(row)
        for key in ("steps_json", "excluded_json", "diagnostics_json"):
            if out.get(key):
                out[key.removesuffix("_json")] = json.loads(out[key])
            else:
                out[key.removesuffix("_json")] = []
        return out
