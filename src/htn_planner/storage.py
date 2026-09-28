"""SQLite-backed evidence store.

Persists each planning run and its full audit trail: the retained expansion
tree, the terminal failure evidence, abandoned backtracking branches, and a
key-step audit log keyed by request identity.  SQLite is used in WAL-ish local
mode; tests point ``HTN_DB_PATH`` at a temporary file.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import PlanResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    request_id        TEXT PRIMARY KEY,
    domain            TEXT NOT NULL,
    domain_version    TEXT NOT NULL,
    problem           TEXT NOT NULL,
    goal              TEXT NOT NULL,
    feasible          INTEGER NOT NULL,
    terminal_failure  TEXT,
    depth_used        INTEGER NOT NULL,
    expansions_used   INTEGER NOT NULL,
    uncertainty_json  TEXT NOT NULL,
    warnings_json     TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS failures (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id    TEXT NOT NULL,
    branch        TEXT NOT NULL,            -- 'terminal' | 'abandoned'
    kind          TEXT NOT NULL,
    task          TEXT,
    method        TEXT,
    detail        TEXT NOT NULL,
    depth         INTEGER,
    node_path_json TEXT NOT NULL,
    extra_json    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    request_id    TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    kind          TEXT NOT NULL,
    task          TEXT NOT NULL,
    args_json     TEXT NOT NULL,
    method        TEXT,
    primitive     TEXT,
    depth         INTEGER NOT NULL,
    status        TEXT NOT NULL,
    detail        TEXT,
    children_json TEXT NOT NULL,
    after_json    TEXT NOT NULL,
    PRIMARY KEY (request_id, node_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL,
    step        INTEGER NOT NULL,
    location    TEXT NOT NULL,
    message     TEXT NOT NULL,
    at          TEXT NOT NULL
);
"""


class EvidenceStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes -----------------------------------------------------------
    def save(self, result: PlanResult, audit: list[dict]) -> None:
        """Persist a complete run atomically."""
        now = datetime.now(timezone.utc).isoformat()
        terminal = result.failures[-1].kind.value if result.failures else None
        goal = f"{result.goal_task}({', '.join(result.goal_args)})"
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO plans (
                    request_id, domain, domain_version, problem, goal, feasible,
                    terminal_failure, depth_used, expansions_used,
                    uncertainty_json, warnings_json, payload_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    result.request_id, result.domain, result.domain_version,
                    result.problem, goal, int(result.feasible), terminal,
                    result.depth_used, result.expansions_used,
                    json.dumps(result.uncertainty), json.dumps(result.warnings),
                    result.model_dump_json(), now,
                ),
            )
            self._conn.execute(
                "DELETE FROM failures WHERE request_id=?", (result.request_id,)
            )
            for ev in result.failures:
                self._insert_failure(result.request_id, "terminal", ev)
            for ev in result.abandoned_branches:
                self._insert_failure(result.request_id, "abandoned", ev)

            self._conn.execute(
                "DELETE FROM nodes WHERE request_id=?", (result.request_id,)
            )
            for node in result.nodes.values():
                self._conn.execute(
                    """INSERT OR REPLACE INTO nodes VALUES
                       (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        result.request_id, node.node_id, node.kind, node.task,
                        json.dumps(node.args), node.method, node.primitive,
                        node.depth, node.status, node.detail,
                        json.dumps(node.children), json.dumps(node.after),
                    ),
                )

            self._conn.execute(
                "DELETE FROM audit_log WHERE request_id=?", (result.request_id,)
            )
            for i, entry in enumerate(audit):
                self._conn.execute(
                    "INSERT INTO audit_log (request_id, step, location, message, at)"
                    " VALUES (?,?,?,?,?)",
                    (
                        result.request_id, i, entry.get("location", ""),
                        entry["message"], entry.get("at", now),
                    ),
                )
            self._conn.commit()

    def _insert_failure(self, request_id: str, branch: str, ev) -> None:
        extra = {
            "tried_methods": ev.tried_methods,
            "rejected_guards": ev.rejected_guards,
        }
        self._conn.execute(
            "INSERT INTO failures (request_id, branch, kind, task, method,"
            " detail, depth, node_path_json, extra_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                request_id, branch, ev.kind.value, ev.task, ev.method,
                ev.detail, ev.depth, json.dumps(ev.node_path), json.dumps(extra),
            ),
        )

    # -- reads ------------------------------------------------------------
    def get_payload(self, request_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT payload_json FROM plans WHERE request_id=?", (request_id,)
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def get_summary(self, request_id: str) -> dict | None:
        row = self._conn.execute(
            """SELECT request_id, domain, domain_version, problem, goal,
                      feasible, terminal_failure, depth_used, expansions_used,
                      uncertainty_json, warnings_json, created_at
               FROM plans WHERE request_id=?""",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "request_id": row["request_id"],
            "domain": row["domain"],
            "domain_version": row["domain_version"],
            "problem": row["problem"],
            "goal": row["goal"],
            "feasible": bool(row["feasible"]),
            "terminal_failure": row["terminal_failure"],
            "depth_used": row["depth_used"],
            "expansions_used": row["expansions_used"],
            "uncertainty": json.loads(row["uncertainty_json"]),
            "warnings": json.loads(row["warnings_json"]),
            "created_at": row["created_at"],
        }

    def list_plans(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            """SELECT request_id, domain, problem, feasible, terminal_failure,
                      created_at FROM plans ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_failures(self, request_id: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT branch, kind, task, method, detail, depth,
                      node_path_json, extra_json
               FROM failures WHERE request_id=? ORDER BY id""",
            (request_id,),
        ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "branch": r["branch"],
                    "kind": r["kind"],
                    "task": r["task"],
                    "method": r["method"],
                    "detail": r["detail"],
                    "depth": r["depth"],
                    "node_path": json.loads(r["node_path_json"]),
                    **json.loads(r["extra_json"]),
                }
            )
        return out

    def get_tree(self, request_id: str) -> dict | None:
        plan = self.get_summary(request_id)
        if plan is None:
            return None
        rows = self._conn.execute(
            """SELECT node_id, kind, task, args_json, method, primitive, depth,
                      status, detail, children_json, after_json
               FROM nodes WHERE request_id=? ORDER BY node_id""",
            (request_id,),
        ).fetchall()
        nodes = [
            {
                "node_id": r["node_id"],
                "kind": r["kind"],
                "task": r["task"],
                "args": json.loads(r["args_json"]),
                "method": r["method"],
                "primitive": r["primitive"],
                "depth": r["depth"],
                "status": r["status"],
                "detail": r["detail"],
                "children": json.loads(r["children_json"]),
                "after": json.loads(r["after_json"]),
            }
            for r in rows
        ]
        payload = self.get_payload(request_id) or {}
        return {
            "request_id": request_id,
            "feasible": plan["feasible"],
            "roots": payload.get("roots", []),
            "execution_order": payload.get("execution_order", []),
            "nodes": nodes,
        }

    def get_audit(self, request_id: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT step, location, message, at FROM audit_log
               WHERE request_id=? ORDER BY step""",
            (request_id,),
        ).fetchall()
        return [dict(r) for r in rows]
