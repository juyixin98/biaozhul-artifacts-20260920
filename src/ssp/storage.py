"""SQLite persistence for planning runs.

A run row stores: run identity, input fingerprint, the full request spec, the
planner verdict (JSON), and the numerical-stack version snapshot.  Failures
are stored too, with their explicit error category - an unknown/failed state is
never written as a success.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import Settings, get_settings
from .errors import PersistenceError, PlannerError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,
    endpoint          TEXT NOT NULL,
    status            TEXT NOT NULL,
    fingerprint       TEXT NOT NULL,
    request_spec      TEXT NOT NULL,
    result_json       TEXT,
    error_category    TEXT,
    error_message     TEXT,
    versions_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at);
CREATE INDEX IF NOT EXISTS idx_runs_fp ON runs(fingerprint);
"""


class RunStore:
    """Thin sqlite3 wrapper; one connection per call (file-local, low volume)."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.path: Path = self.settings.db_path

    def initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
        except (sqlite3.Error, OSError) as exc:
            raise PersistenceError(
                "could not initialize run store", details={"path": str(self.path), "error": str(exc)}
            ) from exc

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def save_success(self, record: dict[str, Any], request_spec: dict[str, Any]) -> None:
        plan = record["plan"]
        self._insert(
            run_id=plan["run_id"],
            endpoint=plan["endpoint"],
            status="completed",
            fingerprint=plan["input_fingerprint"],
            request_spec=request_spec,
            result_json=record,
            error_category=None,
            error_message=None,
            versions=plan.get("versions", {}),
        )

    def save_failure(
        self,
        run_id: str,
        endpoint: str,
        fingerprint: str,
        request_spec: dict[str, Any],
        error: PlannerError,
        versions: dict[str, str],
    ) -> None:
        self._insert(
            run_id=run_id,
            endpoint=endpoint,
            status="failed",
            fingerprint=fingerprint,
            request_spec=request_spec,
            result_json=None,
            error_category=error.category.value,
            error_message=str(error),
            versions=versions,
        )

    def _insert(self, **row: Any) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO runs
                      (run_id, created_at, endpoint, status, fingerprint,
                       request_spec, result_json, error_category, error_message, versions_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["run_id"],
                        datetime.now(timezone.utc).isoformat(),
                        row["endpoint"],
                        row["status"],
                        row["fingerprint"],
                        json.dumps(row["request_spec"], sort_keys=True, default=str),
                        json.dumps(row["result_json"], default=str) if row["result_json"] is not None else None,
                        row["error_category"],
                        row["error_message"],
                        json.dumps(row["versions"], sort_keys=True),
                    ),
                )
        except sqlite3.Error as exc:
            raise PersistenceError(
                "could not persist run", details={"run_id": row["run_id"], "error": str(exc)}
            ) from exc

    def get(self, run_id: str) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                cur = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
                row = cur.fetchone()
        except sqlite3.Error as exc:
            raise PersistenceError("could not read run", details={"run_id": run_id}) from exc
        return self._row_to_dict(row) if row is not None else None

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (int(limit),)
                )
                rows: Iterable[sqlite3.Row] = cur.fetchall()
        except sqlite3.Error as exc:
            raise PersistenceError("could not list runs") from exc
        return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "endpoint": row["endpoint"],
            "status": row["status"],
            "fingerprint": row["fingerprint"],
            "request_spec": json.loads(row["request_spec"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error_category": row["error_category"],
            "error_message": row["error_message"],
            "versions": json.loads(row["versions_json"]) if row["versions_json"] else {},
        }
