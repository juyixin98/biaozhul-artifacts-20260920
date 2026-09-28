"""Evidence repository: persist runs, timeline events, violations.

Every stored record carries:

* ``run_id`` -- a unique execution identity (timestamp + uuid4) that test
  logs correlate against;
* ``input_fingerprint`` -- SHA-256 over the canonical JSON of the exact
  problem (and, where applicable, plan) input, so two runs can be proven
  to have operated on identical inputs;
* ``engine_version`` -- the semantic engine version that produced it.

Nothing here ever converts an unknown/failed state into success: the
persisted ``status``/``outcome``/``optimal`` columns mirror exactly what
the engine returned.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from app import __version__
from app.rules.models import Plan, Problem, ReplayResult, SearchResult


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(problem: Problem, plan: Plan | None = None) -> str:
    parts = [problem.model_dump(mode="json")]
    if plan is not None:
        parts.append(plan.model_dump(mode="json"))
    digest = hashlib.sha256()
    for part in parts:
        digest.update(canonical_json(part).encode("utf-8"))
        digest.update(b"|")
    return digest.hexdigest()


def new_run_id(kind: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{kind}-{stamp}-{uuid.uuid4().hex[:12]}"


class EvidenceStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        # Serializes writes across the ASGI worker threads; reads are
        # safe concurrently under WAL.
        self._write_lock = threading.RLock()

    # ---- writes --------------------------------------------------------

    def save_search(
        self,
        problem: Problem,
        result: SearchResult,
        *,
        kind: str = "solve",
        parent_run_id: str | None = None,
    ) -> str:
        run_id = new_run_id(kind)
        fp = fingerprint(problem, result.plan)
        with self._write_lock:
            self.conn.execute(
                """
                INSERT INTO runs (
                    run_id, parent_run_id, kind, created_at, engine_version,
                    input_fingerprint, problem_name, problem_json, plan_json,
                    status, outcome, optimal, makespan, budget_nodes,
                    nodes_expanded, elapsed_ms, explored_depth, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    parent_run_id,
                    kind,
                    datetime.now(timezone.utc).isoformat(),
                    __version__,
                    fp,
                    problem.name,
                    canonical_json(problem.model_dump(mode="json")),
                    canonical_json(result.plan.model_dump(mode="json")) if result.plan else None,
                    result.status.value,
                    None,
                    1 if result.optimal else 0,
                    result.makespan,
                    result.budget_nodes,
                    result.nodes_expanded,
                    result.elapsed_ms,
                    result.explored_depth,
                    result.reason,
                ),
            )
            self.conn.commit()
        return run_id

    def save_replay(
        self,
        problem: Problem,
        plan: Plan | None,
        replay_result: ReplayResult,
        *,
        kind: str = "replay",
        parent_run_id: str | None = None,
    ) -> str:
        run_id = new_run_id(kind)
        fp = fingerprint(problem, plan)
        with self._write_lock:
            self.conn.execute(
                """
                INSERT INTO runs (
                    run_id, parent_run_id, kind, created_at, engine_version,
                    input_fingerprint, problem_name, problem_json, plan_json,
                    status, outcome, optimal, makespan, budget_nodes,
                    nodes_expanded, elapsed_ms, explored_depth, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    parent_run_id,
                    kind,
                    datetime.now(timezone.utc).isoformat(),
                    __version__,
                    fp,
                    problem.name,
                    canonical_json(problem.model_dump(mode="json")),
                    canonical_json(plan.model_dump(mode="json")) if plan else None,
                    replay_result.outcome.value,
                    replay_result.outcome.value,
                    0,
                    max((s.start + s.duration for s in plan.steps), default=0) if plan else None,
                    None,
                    None,
                    None,
                    None,
                    f"goal_satisfied={replay_result.goal_satisfied}; "
                    f"{len(replay_result.violations)} violation(s)",
                ),
            )
            for seq, event in enumerate(replay_result.events):
                self.conn.execute(
                    """
                    INSERT INTO timeline_events (
                        run_id, seq, time, phase_order, kind, action, detail,
                        state_before_json, state_after_json, active_actions_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        seq,
                        event.time,
                        event.order,
                        event.kind.value,
                        event.action,
                        event.detail,
                        canonical_json(event.state_before),
                        canonical_json(event.state_after),
                        canonical_json(event.active_actions),
                    ),
                )
            for seq, violation in enumerate(replay_result.violations):
                self.conn.execute(
                    """
                    INSERT INTO violations (
                        run_id, seq, category, time, action, resource, message, detail
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        seq,
                        violation.category,
                        violation.time,
                        violation.action,
                        violation.resource,
                        violation.message,
                        violation.detail,
                    ),
                )
            self.conn.commit()
        return run_id

    def save_reference_check(
        self,
        run_id: str,
        *,
        reference_kind: str,
        reference_found: bool,
        reference_makespan: int | None,
        solver_found: bool,
        solver_makespan: int | None,
        agree: bool,
        schedules_evaluated: int,
        detail: str,
    ) -> None:
        with self._write_lock:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO reference_checks (
                    run_id, reference_kind, reference_found, reference_makespan,
                    solver_found, solver_makespan, agree, schedules_evaluated, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    reference_kind,
                    1 if reference_found else 0,
                    reference_makespan,
                    1 if solver_found else 0,
                    solver_makespan,
                    1 if agree else 0,
                    schedules_evaluated,
                    detail,
                ),
            )
            self.conn.commit()

    # ---- reads ---------------------------------------------------------

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, *, limit: int = 50, kind: str | None = None) -> list[dict[str, Any]]:
        if kind is not None:
            rows = self.conn.execute(
                "SELECT * FROM runs WHERE kind = ? ORDER BY created_at DESC LIMIT ?", (kind, limit)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_events(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM timeline_events WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_violations(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM violations WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_reference_check(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM reference_checks WHERE run_id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row else None
