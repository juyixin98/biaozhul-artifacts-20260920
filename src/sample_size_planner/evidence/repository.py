"""SQLite persistence for reproducible runs.

The schema is intentionally denormalised towards auditability: every stored
plan/simulation/interim row carries the ``run_id`` of the run that produced it
plus the full JSON request/result envelope, so a result can be reconstructed
and re-run from the stored inputs alone.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    purpose     TEXT NOT NULL,
    versions    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    family          TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    success         INTEGER NOT NULL,
    failure_category TEXT,
    n_group0        INTEGER,
    n_group1        INTEGER,
    n_total         INTEGER,
    achieved_power  REAL,
    method          TEXT,
    result_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS simulations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    scenario        TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    replications    INTEGER NOT NULL,
    estimated_power REAL NOT NULL,
    ci_low          REAL NOT NULL,
    ci_high         REAL NOT NULL,
    agrees          INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interim_plans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    family          TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    schedule_json   TEXT NOT NULL,
    final_size      REAL NOT NULL,
    alt_power       REAL NOT NULL,
    created_at      TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunRepository:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA foreign_keys = ON")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def start_run(self, run_id: str, created_at: str, purpose: str, versions: Dict[str, str]) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO runs(run_id, created_at, purpose, versions) VALUES (?,?,?,?)",
                (run_id, created_at, purpose, json.dumps(versions, sort_keys=True)),
            )

    def save_plan(self, run_id: str, family: str, request: Dict[str, Any],
                  result: Dict[str, Any]) -> int:
        with self.connect() as con:
            cur = con.execute(
                """INSERT INTO plans(run_id, family, request_json, success, failure_category,
                       n_group0, n_group1, n_total, achieved_power, method, result_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, family, json.dumps(request, sort_keys=True),
                 1 if result.get("success") else 0,
                 result.get("failure_category"), result.get("n_per_group0"),
                 result.get("n_per_group1"), result.get("n_total"),
                 result.get("achieved_power"), result.get("method"),
                 json.dumps(result, sort_keys=True, default=str), _now()),
            )
            return int(cur.lastrowid)

    def save_simulation(self, run_id: str, scenario: str, request: Dict[str, Any],
                        replications: int, estimated_power: float, ci_low: float,
                        ci_high: float, agrees: bool) -> int:
        with self.connect() as con:
            cur = con.execute(
                """INSERT INTO simulations(run_id, scenario, request_json, replications,
                       estimated_power, ci_low, ci_high, agrees, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (run_id, scenario, json.dumps(request, sort_keys=True), replications,
                 estimated_power, ci_low, ci_high, 1 if agrees else 0, _now()),
            )
            return int(cur.lastrowid)

    def save_interim(self, run_id: str, family: str, request: Dict[str, Any],
                     schedule: Dict[str, Any], final_size: float, alt_power: float) -> int:
        with self.connect() as con:
            cur = con.execute(
                """INSERT INTO interim_plans(run_id, family, request_json, schedule_json,
                       final_size, alt_power, created_at) VALUES (?,?,?,?,?,?,?)""",
                (run_id, family, json.dumps(request, sort_keys=True),
                 json.dumps(schedule, sort_keys=True, default=str),
                 final_size, alt_power, _now()),
            )
            return int(cur.lastrowid)

    def list_plans(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM plans"
        params: tuple = ()
        if run_id:
            sql += " WHERE run_id = ?"
            params = (run_id,)
        sql += " ORDER BY id"
        with self.connect() as con:
            return [dict(r) for r in con.execute(sql, params).fetchall()]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as con:
            row = con.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return dict(row) if row else None
