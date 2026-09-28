"""SQLite evidence store.

Every planning request gets a ``run_id`` and persists enough structured
evidence to replay the question without the original client:

    runs            - one row per request: request payload, status, timing,
                      failure category/code, search verdict and counters.
    run_steps       - independently re-executed plan steps, with the full
                      state before and after each step.
    run_trace       - key intermediate search states (bounded sample),
                      with g/h/f, depth and the generating action.
    run_errors      - the structured error envelope, when a run failed.

State columns store the canonical encoding from ``encoding`` module, so a
row can be decoded back into a state exactly (see :func:`replay_request`).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .encoding import decode_state, encode_state

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    created_at          REAL NOT NULL,
    domain_name         TEXT,
    problem_name        TEXT,
    algorithm           TEXT,
    heuristic           TEXT,
    status              TEXT NOT NULL,
    result_status       TEXT,
    reason              TEXT,
    optimal_guarantee   INTEGER,
    cost                INTEGER,
    path_length         INTEGER,
    expanded            INTEGER,
    generated           INTEGER,
    frontier_peak       INTEGER,
    elapsed_seconds     REAL,
    request_json        TEXT NOT NULL,
    plan_json           TEXT,
    verification_json   TEXT
);
CREATE TABLE IF NOT EXISTS run_steps (
    run_id      TEXT NOT NULL,
    step_index  INTEGER NOT NULL,
    action      TEXT NOT NULL,
    cost        INTEGER NOT NULL,
    state_before TEXT NOT NULL,
    state_after  TEXT NOT NULL,
    PRIMARY KEY (run_id, step_index)
);
CREATE TABLE IF NOT EXISTS run_trace (
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    depth       INTEGER NOT NULL,
    g           INTEGER,
    h           REAL,
    f           REAL,
    action      TEXT,
    state_hash  TEXT NOT NULL,
    state_enc   TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS run_errors (
    run_id      TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    code        TEXT NOT NULL,
    message     TEXT NOT NULL,
    details_json TEXT NOT NULL
);
"""


class EvidenceStore:
    """Thread-safe wrapper around a single SQLite file."""

    def __init__(self, path: str = "data/evidence.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #

    def new_run_id(self) -> str:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        return f"run-{stamp}-{uuid.uuid4().hex[:8]}"

    def insert_run(
        self,
        *,
        run_id: str,
        request: dict[str, Any],
        domain_name: Optional[str],
        problem_name: Optional[str],
        algorithm: Optional[str],
        heuristic: Optional[str],
        status: str,
        search_dict: Optional[dict[str, Any]] = None,
        plan: Optional[list[dict[str, Any]]] = None,
        verification: Optional[dict[str, Any]] = None,
        elapsed_seconds: float,
    ) -> None:
        sd = search_dict or {}
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runs (
                    run_id, created_at, domain_name, problem_name,
                    algorithm, heuristic, status, result_status, reason,
                    optimal_guarantee, cost, path_length, expanded, generated,
                    frontier_peak, elapsed_seconds, request_json, plan_json,
                    verification_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    time.time(),
                    domain_name,
                    problem_name,
                    algorithm,
                    heuristic,
                    status,
                    sd.get("status"),
                    sd.get("reason"),
                    None if sd.get("optimal_guarantee") is None
                    else int(sd["optimal_guarantee"]),
                    sd.get("cost"),
                    sd.get("path_length"),
                    sd.get("expanded"),
                    sd.get("generated"),
                    sd.get("frontier_peak"),
                    elapsed_seconds,
                    json.dumps(request, ensure_ascii=False, sort_keys=True),
                    None if plan is None
                    else json.dumps(plan, ensure_ascii=False),
                    None if verification is None
                    else json.dumps(verification, ensure_ascii=False),
                ),
            )
            self._conn.commit()

    def insert_error(
        self,
        *,
        run_id: str,
        category: str,
        code: str,
        message: str,
        details: list[dict[str, Any]],
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO run_errors
                    (run_id, category, code, message, details_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, category, code, message,
                 json.dumps(details, ensure_ascii=False)),
            )
            self._conn.commit()

    def insert_steps(self, run_id: str, steps: list[dict[str, Any]]) -> None:
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO run_steps
                    (run_id, step_index, action, cost,
                     state_before, state_after)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        s["index"],
                        s["action"],
                        s["cost"],
                        encode_state(decode_texts(s["state_before"])),
                        encode_state(decode_texts(s["state_after"])),
                    )
                    for s in steps
                ],
            )
            self._conn.commit()

    def insert_trace(self, run_id: str, trace: list[dict[str, Any]]) -> None:
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO run_trace
                    (run_id, seq, depth, g, h, f, action,
                     state_hash, state_enc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        t["seq"],
                        t["depth"],
                        t["g"],
                        t["h"],
                        t["f"],
                        t["action"],
                        t["state_hash"],
                        encode_state(decode_texts(t["state"])),
                    )
                    for t in trace
                ],
            )
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # Reads / replay
    # ------------------------------------------------------------------ #

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        for key in ("request_json", "plan_json", "verification_json"):
            record[key] = (
                json.loads(record[key]) if record[key] is not None else None
            )
        error_row = None
        with self._lock:
            error_row = self._conn.execute(
                "SELECT * FROM run_errors WHERE run_id = ?", (run_id,)
            ).fetchone()
        if error_row is not None:
            err = dict(error_row)
            err["details"] = json.loads(err.pop("details_json"))
            record["error"] = err
        return record

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT run_id, created_at, domain_name, problem_name,
                       algorithm, heuristic, status, result_status, reason,
                       cost, path_length, expanded, elapsed_seconds
                FROM runs ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_steps(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT step_index, action, cost, state_before, state_after
                FROM run_steps WHERE run_id = ? ORDER BY step_index
                """,
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            before = decode_state(row["state_before"])
            after = decode_state(row["state_after"])
            from .model import atom_text

            result.append({
                "index": row["step_index"],
                "action": row["action"],
                "cost": row["cost"],
                "state_before": [atom_text(a) for a in sorted(before)],
                "state_after": [atom_text(a) for a in sorted(after)],
            })
        return result

    def get_trace(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT seq, depth, g, h, f, action, state_hash, state_enc
                FROM run_trace WHERE run_id = ? ORDER BY seq
                """,
                (run_id,),
            ).fetchall()
        from .model import atom_text

        return [
            {
                "seq": r["seq"],
                "depth": r["depth"],
                "g": r["g"],
                "h": r["h"],
                "f": r["f"],
                "action": r["action"],
                "state_hash": r["state_hash"],
                "state": [atom_text(a)
                          for a in sorted(decode_state(r["state_enc"]))],
            }
            for r in rows
        ]

    def replay_request(self, run_id: str) -> Optional[dict[str, Any]]:
        """Return the original request body stored for a run."""
        record = self.get_run(run_id)
        return None if record is None else record["request_json"]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def decode_texts(texts: list[str]):
    """Turn ``['pred(a,b)', ...]`` back into an atom frozenset."""
    from .parser import _parse_literal  # reuse the single literal grammar

    return frozenset(_parse_literal(t, "evidence")[0] for t in texts)
