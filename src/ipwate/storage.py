"""SQLite persistence for run evidence records.

Only the serialized result (no raw observations) is stored, keyed by
request_id. This is a local, dependency-free audit trail.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .errors import StorageError
from .statcontract import EstimationResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    request_id TEXT PRIMARY KEY,
    verdict TEXT NOT NULL,
    estimand TEXT NOT NULL,
    n_observations INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    result_json TEXT NOT NULL
);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def save_run(db_path: str, result: EstimationResult) -> None:
    try:
        with _connect(db_path) as conn:
            conn.execute(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO runs "
                "(request_id, verdict, estimand, n_observations, result_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    result.request_id,
                    result.verdict,
                    result.contract.estimand,
                    result.n_observations,
                    result.to_json(),
                ),
            )
    except (sqlite3.Error, OSError) as exc:
        raise StorageError(f"Failed to persist run: {exc}") from exc


def load_run(db_path: str, request_id: str) -> dict | None:
    try:
        with _connect(db_path) as conn:
            conn.execute(_SCHEMA)
            row = conn.execute(
                "SELECT result_json FROM runs WHERE request_id = ?", (request_id,)
            ).fetchone()
    except (sqlite3.Error, OSError) as exc:
        raise StorageError(f"Failed to read run: {exc}") from exc
    return json.loads(row["result_json"]) if row else None
