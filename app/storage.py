"""SQLite persistence of requests and decision records.

Only metadata and decisions are stored -- never the raw observations. The
payload fingerprint ties a row to a dataset without keeping the dataset.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from .contracts import DecisionRecord, EstimateResponse

_SCHEMA = """
CREATE TABLE IF NOT EXISTS estimate_runs (
    request_id      TEXT PRIMARY KEY,
    fingerprint     TEXT NOT NULL,
    n_obs           INTEGER NOT NULL,
    n_endogenous    INTEGER NOT NULL,
    n_instruments   INTEGER NOT NULL,
    status          TEXT NOT NULL,
    verdict         TEXT NOT NULL,
    failure_category TEXT NOT NULL,
    reasons         TEXT NOT NULL,
    key_state       TEXT NOT NULL,
    assumptions     TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class DecisionStore:
    """Thread-safe thin wrapper over one SQLite file."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, response: EstimateResponse) -> None:
        d: DecisionRecord = response.decision
        row = (
            d.request_id, d.fingerprint, d.n_obs, response.n_endogenous,
            response.n_instruments, response.status, d.verdict.value,
            d.failure_category.value, json.dumps(d.reasons),
            json.dumps(d.key_state),
            json.dumps([a.model_dump() for a in d.assumptions]),
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO estimate_runs (
                    request_id, fingerprint, n_obs, n_endogenous, n_instruments,
                    status, verdict, failure_category, reasons, key_state,
                    assumptions
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            conn.commit()

    def fetch(self, request_id: str) -> dict | None:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM estimate_runs WHERE request_id = ?", (request_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        record = dict(row)
        for key in ("reasons", "key_state", "assumptions"):
            record[key] = json.loads(record[key])
        return record

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "SELECT request_id, fingerprint, n_obs, status, verdict, "
                "failure_category, created_at FROM estimate_runs "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            )
            rows: Iterable[sqlite3.Row] = cur.fetchall()
        return [dict(r) for r in rows]
