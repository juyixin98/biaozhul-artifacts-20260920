"""SQLite-backed index for rulesets, diagnostics and run logs.

Schema (created idempotently on open):

- ``rulesets(id, name UNIQUE, created_at, spec_json, diagnostics_json)``
- ``run_logs(seq, run_id, ts, op, stage, detail_json)`` — insertion order is
  the replay order for a run.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from ..errors import state_conflict
from .models import RuleSetRecord, RunLogEntry

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rulesets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    diagnostics_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_logs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    op TEXT NOT NULL,
    stage TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_logs_run_id ON run_logs(run_id);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    # -- rulesets ------------------------------------------------------------
    def insert_ruleset(self, record: RuleSetRecord) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO rulesets (id, name, created_at, spec_json, "
                    "diagnostics_json) VALUES (?, ?, ?, ?, ?)",
                    (
                        record.id,
                        record.name,
                        record.created_at,
                        json.dumps(record.spec),
                        json.dumps(record.diagnostics),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise state_conflict(
                f"a ruleset named {record.name!r} already exists",
                detail={"name": record.name},
            ) from exc

    def get_ruleset(self, ruleset_id: str) -> RuleSetRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, created_at, spec_json, diagnostics_json "
                "FROM rulesets WHERE id = ?",
                (ruleset_id,),
            ).fetchone()
        if row is None:
            return None
        return RuleSetRecord(
            id=row[0],
            name=row[1],
            created_at=row[2],
            spec=json.loads(row[3]),
            diagnostics=json.loads(row[4]),
        )

    # -- run logs --------------------------------------------------------------
    def add_log(self, entry: RunLogEntry) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO run_logs (run_id, ts, op, stage, detail_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    entry.run_id,
                    entry.ts,
                    entry.op,
                    entry.stage,
                    json.dumps(entry.detail),
                ),
            )

    def get_logs(self, run_id: str) -> list[RunLogEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_id, ts, op, stage, detail_json FROM run_logs "
                "WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
        return [
            RunLogEntry(
                run_id=row[0],
                ts=row[1],
                op=row[2],
                stage=row[3],
                detail=json.loads(row[4]),
            )
            for row in rows
        ]
