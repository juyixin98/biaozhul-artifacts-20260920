"""Evidence store: SQLite-backed record of runs, events, and solutions.

Every solve produces one run row (with problem, config, and tool versions),
a stream of ordered events (prunings with reasons, branches, backtracks,
Hall certificates, budget exhaustion), and the solutions found. Events from
branches later abandoned are kept — they are namespaced by node id and depth
so the search tree, including restored-away subtrees, stays auditable.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    problem_name TEXT NOT NULL,
    problem_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    versions_json TEXT NOT NULL,
    status TEXT NOT NULL,
    partial INTEGER NOT NULL,
    stats_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
CREATE TABLE IF NOT EXISTS solutions (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    idx INTEGER NOT NULL,
    assignment_json TEXT NOT NULL,
    PRIMARY KEY (run_id, idx)
);
"""


class EvidenceStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        # check_same_thread=False: the ASGI server serves requests from a
        # worker thread; the lock serializes access instead.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def new_run_id(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return f"run-{stamp}-{uuid.uuid4().hex[:8]}"

    def save_run(
        self,
        *,
        run_id: str,
        problem_name: str,
        problem: dict,
        config: dict,
        versions: dict,
        status: str,
        partial: bool,
        stats: dict,
        events: list[dict],
        solutions: list[dict],
    ) -> None:
        with self._lock:
            self._save_run_locked(
                run_id=run_id, problem_name=problem_name, problem=problem,
                config=config, versions=versions, status=status,
                partial=partial, stats=stats, events=events, solutions=solutions,
            )

    def _save_run_locked(
        self, *, run_id, problem_name, problem, config, versions, status,
        partial, stats, events, solutions,
    ) -> None:
        conn = self._conn
        conn.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                datetime.now(timezone.utc).isoformat(),
                problem_name,
                json.dumps(problem, sort_keys=True),
                json.dumps(config, sort_keys=True),
                json.dumps(versions, sort_keys=True),
                status,
                int(partial),
                json.dumps(stats, sort_keys=True),
            ),
        )
        conn.executemany(
            "INSERT INTO events(run_id, seq, kind, payload_json) VALUES (?,?,?,?)",
            [
                (
                    run_id,
                    e["seq"],
                    e["kind"],
                    json.dumps({k: v for k, v in e.items() if k not in ("seq", "kind")},
                               sort_keys=True),
                )
                for e in events
            ],
        )
        conn.executemany(
            "INSERT INTO solutions VALUES (?,?,?)",
            [
                (run_id, i, json.dumps(s, sort_keys=True))
                for i, s in enumerate(solutions)
            ],
        )
        conn.commit()

    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "problem_name": row["problem_name"],
            "problem": json.loads(row["problem_json"]),
            "config": json.loads(row["config_json"]),
            "versions": json.loads(row["versions_json"]),
            "status": row["status"],
            "partial": bool(row["partial"]),
            "stats": json.loads(row["stats_json"]),
        }

    def get_events(self, run_id: str, kind: str | None = None) -> list[dict]:
        with self._lock:
            if kind is None:
                rows = self._conn.execute(
                    "SELECT seq, kind, payload_json FROM events WHERE run_id = ? ORDER BY seq",
                    (run_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT seq, kind, payload_json FROM events WHERE run_id = ? AND kind = ? ORDER BY seq",
                    (run_id, kind),
                ).fetchall()
        return [
            {"seq": r["seq"], "kind": r["kind"], **json.loads(r["payload_json"])}
            for r in rows
        ]

    def get_solutions(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT assignment_json FROM solutions WHERE run_id = ? ORDER BY idx",
                (run_id,),
            ).fetchall()
        return [json.loads(r["assignment_json"]) for r in rows]
