"""Evidence store: durable SQLite record of every planning run.

Each run persists enough to reproduce a verdict independently:

* the exact problem JSON submitted (input evidence + run identity),
* solver status, explicit failure code, budgets used and nodes expanded,
* the accepted schedule and the **full replayed timeline** (every event with
  state/resource snapshots),
* the search trace (plan-found and pruning events with decision reasons).

Nothing is ever recorded as a blanket success: invalid requests get an
``invalid_problem`` row carrying the parser message; solver failures carry
their specific ``FailureCode``.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model import Problem, ScheduledAction, TimelineEvent
from .simulator import SimulationOutcome
from .solver import SolveResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    created_at       TEXT NOT NULL,
    status           TEXT NOT NULL,
    failure_code     TEXT,
    horizon          INTEGER NOT NULL,
    node_budget      INTEGER NOT NULL,
    time_budget_s    REAL NOT NULL,
    nodes_expanded   INTEGER NOT NULL,
    best_cost        INTEGER,
    goal_time        INTEGER,
    message          TEXT NOT NULL,
    problem_json     TEXT NOT NULL,
    problem_sha256   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_schedule (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL REFERENCES runs(run_id),
    seq       INTEGER NOT NULL,
    action_id TEXT NOT NULL,
    start     INTEGER NOT NULL,
    end       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS run_timeline (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL REFERENCES runs(run_id),
    t          INTEGER NOT NULL,
    ord        INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    action_id  TEXT NOT NULL,
    state_json TEXT NOT NULL,
    resources_json TEXT NOT NULL,
    note       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_trace (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL REFERENCES runs(run_id),
    seq        INTEGER NOT NULL,
    entry_json TEXT NOT NULL
);
"""


class EvidenceStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "EvidenceStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def new_run_id() -> str:
        return f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:12]}"

    def save_run(
        self,
        run_id: str,
        problem_raw: dict[str, Any],
        problem: Problem,
        result: SolveResult,
        replay: SimulationOutcome | None,
        node_budget: int,
        time_budget_s: float,
    ) -> None:
        import hashlib

        payload = json.dumps(problem_raw, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO runs (run_id, created_at, status, failure_code, horizon,
                               node_budget, time_budget_s, nodes_expanded,
                               best_cost, goal_time, message, problem_json, problem_sha256)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                datetime.now(timezone.utc).isoformat(),
                result.status,
                result.failure_code.value if result.failure_code else None,
                problem.horizon,
                node_budget,
                time_budget_s,
                result.nodes_expanded,
                result.best_cost,
                result.goal_time,
                result.message,
                json.dumps(problem_raw, sort_keys=True),
                digest,
            ),
        )
        for seq, sa in enumerate(result.schedule):
            cur.execute(
                "INSERT INTO run_schedule (run_id, seq, action_id, start, end) VALUES (?,?,?,?,?)",
                (run_id, seq, sa.action_id, sa.start, sa.end),
            )
        if replay is not None and replay.ok:
            for ev in replay.events:
                cur.execute(
                    """
                    INSERT INTO run_timeline
                        (run_id, t, ord, kind, action_id, state_json, resources_json, note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        ev.time,
                        ev.order,
                        ev.kind.value,
                        ev.action_id,
                        json.dumps(ev.state, sort_keys=True),
                        json.dumps(ev.resources, sort_keys=True),
                        ev.note,
                    ),
                )
        for seq, entry in enumerate(result.trace):
            cur.execute(
                "INSERT INTO run_trace (run_id, seq, entry_json) VALUES (?, ?, ?)",
                (run_id, seq, json.dumps(entry, sort_keys=True, default=str)),
            )
        self._conn.commit()

    def save_invalid(self, run_id: str, problem_raw: Any, message: str, node_budget: int,
                     time_budget_s: float, horizon: int | None) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO runs (run_id, created_at, status, failure_code, horizon,
                               node_budget, time_budget_s, nodes_expanded,
                               best_cost, goal_time, message, problem_json, problem_sha256)
            VALUES (?, ?, 'invalid', 'invalid_problem', ?, ?, ?, 0, NULL, NULL, ?, ?, '')
            """,
            (
                run_id,
                datetime.now(timezone.utc).isoformat(),
                horizon if horizon is not None else -1,
                node_budget,
                time_budget_s,
                message,
                json.dumps(problem_raw, sort_keys=True, default=str),
            ),
        )
        self._conn.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        schedule = [
            {"action_id": r["action_id"], "start": r["start"], "end": r["end"]}
            for r in self._conn.execute(
                "SELECT action_id, start, end FROM run_schedule WHERE run_id=? ORDER BY seq",
                (run_id,),
            )
        ]
        timeline = [
            {
                "time": r["t"],
                "order": r["ord"],
                "kind": r["kind"],
                "action_id": r["action_id"],
                "state": json.loads(r["state_json"]),
                "resources": json.loads(r["resources_json"]),
                "note": r["note"],
            }
            for r in self._conn.execute(
                "SELECT * FROM run_timeline WHERE run_id=? ORDER BY id", (run_id,)
            )
        ]
        trace = [
            json.loads(r["entry_json"])
            for r in self._conn.execute(
                "SELECT entry_json FROM run_trace WHERE run_id=? ORDER BY seq", (run_id,)
            )
        ]
        return {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "status": row["status"],
            "failure_code": row["failure_code"],
            "horizon": row["horizon"],
            "budgets": {
                "node_budget": row["node_budget"],
                "time_budget_seconds": row["time_budget_s"],
                "nodes_expanded": row["nodes_expanded"],
            },
            "best_cost": row["best_cost"],
            "goal_time": row["goal_time"],
            "message": row["message"],
            "problem": json.loads(row["problem_json"]),
            "problem_sha256": row["problem_sha256"],
            "schedule": schedule,
            "timeline": timeline,
            "trace": trace,
        }

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT run_id, created_at, status, failure_code, best_cost, goal_time,
                   nodes_expanded, message
            FROM runs ORDER BY rowid DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
