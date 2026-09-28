"""SQLite evidence store.

Every planning attempt - success, provable unsolvability, bounded unknown,
invalid input - is persisted as one row keyed by a replayable ``run_id``.
The full request, the search outcome, the independent executor trace and the
error envelope are all stored, so a result can be audited or replayed without
the original caller.

The store is intentionally synchronous and tiny (one connection, WAL, a
single writer). It raises :class:`StorageError` for every database failure so
the API can classify it as ``COMPUTATION_FAILED`` rather than crash.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from strips_planner.errors import RunNotFoundError, StorageError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    problem_name        TEXT,
    category            TEXT NOT NULL,
    success             INTEGER NOT NULL,
    status              TEXT NOT NULL,
    algorithm           TEXT,
    heuristic           TEXT,
    optimal             INTEGER,
    path_cost           REAL,
    plan                TEXT,
    request_json        TEXT NOT NULL,
    problem_json        TEXT,
    search_json         TEXT,
    validation_json     TEXT,
    error_json          TEXT,
    stats_json          TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_created ON runs (created_at);
CREATE INDEX IF NOT EXISTS idx_runs_status  ON runs (status);
"""


class EvidenceStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        try:
            self._conn = sqlite3.connect(
                self._path, check_same_thread=False, isolation_level=None
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise StorageError(f"cannot open evidence store at {self._path}: {exc}") from exc

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error as exc:
            raise StorageError(f"cannot close evidence store: {exc}") from exc

    def save_run(self, record: "RunRecord") -> None:
        row = record.to_row()
        placeholders = ", ".join(f":{k}" for k in row)
        try:
            self._conn.execute(
                f"INSERT INTO runs ({', '.join(row)}) VALUES ({placeholders})", row
            )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"run {record.run_id!r} already exists") from exc
        except sqlite3.Error as exc:
            raise StorageError(f"failed to persist run {record.run_id!r}: {exc}") from exc

    def get_run(self, run_id: str) -> dict[str, Any]:
        try:
            cur = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        except sqlite3.Error as exc:
            raise StorageError(f"failed to read run {run_id!r}: {exc}") from exc
        row = cur.fetchone()
        if row is None:
            raise RunNotFoundError(
                f"no run with id {run_id!r}", details={"run_id": run_id}
            )
        return _row_to_dict(row)

    def list_runs(
        self,
        *,
        status: str | None = None,
        category: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 500)))
        try:
            cur = self._conn.execute(
                f"SELECT * FROM runs {where} ORDER BY created_at DESC, run_id DESC LIMIT ?",
                params,
            )
            return [_row_to_dict(r) for r in cur.fetchall()]
        except sqlite3.Error as exc:
            raise StorageError(f"failed to list runs: {exc}") from exc


class RunRecord:
    """Mutable builder for one run row; serialized at save time."""

    def __init__(
        self,
        *,
        run_id: str,
        created_at: str,
        category: str,
        success: bool,
        status: str,
        request: dict[str, Any],
        problem_name: str | None = None,
        algorithm: str | None = None,
        heuristic: str | None = None,
        optimal: bool | None = None,
        path_cost: float | None = None,
        plan: list[str] | None = None,
        problem_summary: dict[str, Any] | None = None,
        search: dict[str, Any] | None = None,
        validation: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        stats: dict[str, Any] | None = None,
    ) -> None:
        self.run_id = run_id
        self.created_at = created_at
        self.category = category
        self.success = success
        self.status = status
        self.request = request
        self.problem_name = problem_name
        self.algorithm = algorithm
        self.heuristic = heuristic
        self.optimal = optimal
        self.path_cost = path_cost
        self.plan = plan
        self.problem_summary = problem_summary
        self.search = search
        self.validation = validation
        self.error = error
        self.stats = stats

    def to_row(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "problem_name": self.problem_name,
            "category": self.category,
            "success": 1 if self.success else 0,
            "status": self.status,
            "algorithm": self.algorithm,
            "heuristic": self.heuristic,
            "optimal": None if self.optimal is None else 1 if self.optimal else 0,
            "path_cost": self.path_cost,
            "plan": _dumps(self.plan),
            "request_json": _dumps(self.request),
            "problem_json": _dumps(self.problem_summary),
            "search_json": _dumps(self.search),
            "validation_json": _dumps(self.validation),
            "error_json": _dumps(self.error),
            "stats_json": _dumps(self.stats),
        }


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "created_at": row["created_at"],
        "problem_name": row["problem_name"],
        "category": row["category"],
        "success": bool(row["success"]),
        "status": row["status"],
        "algorithm": row["algorithm"],
        "heuristic": row["heuristic"],
        "optimal": None if row["optimal"] is None else bool(row["optimal"]),
        "path_cost": row["path_cost"],
        "plan": _loads(row["plan"]),
        "request": _loads(row["request_json"]),
        "problem": _loads(row["problem_json"]),
        "search": _loads(row["search_json"]),
        "validation": _loads(row["validation_json"]),
        "error": _loads(row["error_json"]),
        "stats": _loads(row["stats_json"]),
    }
