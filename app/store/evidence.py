"""SQLite-backed evidence store.

Every solve run is persisted with its model payload, outcome, statistics and
the full pruning reason trace, so results can be audited and replayed.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    model_name TEXT NOT NULL,
    model_json TEXT NOT NULL,
    status TEXT NOT NULL,
    solution_json TEXT,
    stats_json TEXT NOT NULL,
    failure_json TEXT,
    solver_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    seq INTEGER NOT NULL,
    variable TEXT NOT NULL,
    value INTEGER NOT NULL,
    constraint_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reasons_run ON reasons(run_id);
"""


@dataclass
class RunRecord:
    run_id: str
    created_at: str
    model_name: str
    model: dict[str, Any]
    status: str
    solution: dict[str, int] | None
    stats: dict[str, Any]
    failure: dict[str, Any] | None
    solver_version: str


class EvidenceStore:
    """Thread-safe wrapper around a SQLite connection."""

    def __init__(self, path: str = ":memory:"):
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def save_run(
        self,
        run_id: str,
        model: dict[str, Any],
        status: str,
        solution: dict[str, int] | None,
        stats: dict[str, Any],
        failure: dict[str, Any] | None,
        reasons: list[dict[str, Any]],
        solver_version: str,
    ) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._connection.execute(
                "INSERT INTO runs (run_id, created_at, model_name, model_json,"
                " status, solution_json, stats_json, failure_json, solver_version)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    created_at,
                    model.get("name", "unnamed"),
                    json.dumps(model),
                    status,
                    json.dumps(solution) if solution is not None else None,
                    json.dumps(stats),
                    json.dumps(failure) if failure is not None else None,
                    solver_version,
                ),
            )
            self._connection.executemany(
                "INSERT INTO reasons (run_id, seq, variable, value,"
                " constraint_id, kind, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        index,
                        step["variable"],
                        step["value"],
                        step["constraint"],
                        step["kind"],
                        json.dumps(step["detail"]),
                    )
                    for index, step in enumerate(reasons)
                ],
            )
            self._connection.commit()

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return RunRecord(
            run_id=row["run_id"],
            created_at=row["created_at"],
            model_name=row["model_name"],
            model=json.loads(row["model_json"]),
            status=row["status"],
            solution=json.loads(row["solution_json"])
            if row["solution_json"] is not None
            else None,
            stats=json.loads(row["stats_json"]),
            failure=json.loads(row["failure_json"])
            if row["failure_json"] is not None
            else None,
            solver_version=row["solver_version"],
        )

    def get_reasons(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT seq, variable, value, constraint_id, kind, detail_json"
                " FROM reasons WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
        return [
            {
                "seq": row["seq"],
                "variable": row["variable"],
                "value": row["value"],
                "constraint": row["constraint_id"],
                "kind": row["kind"],
                "detail": json.loads(row["detail_json"]),
            }
            for row in rows
        ]

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT run_id, created_at, model_name, status FROM runs"
                " ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
