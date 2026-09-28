"""SQLite schema and connection management for planning evidence."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    parent_run_id       TEXT,
    kind                TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    engine_version      TEXT NOT NULL,
    input_fingerprint   TEXT NOT NULL,
    problem_name        TEXT NOT NULL,
    problem_json        TEXT NOT NULL,
    plan_json           TEXT,
    status              TEXT NOT NULL,
    outcome             TEXT,
    optimal             INTEGER NOT NULL,
    makespan            INTEGER,
    budget_nodes        INTEGER,
    nodes_expanded      INTEGER,
    elapsed_ms          REAL,
    explored_depth      INTEGER,
    reason              TEXT
);

CREATE TABLE IF NOT EXISTS timeline_events (
    run_id              TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    seq                 INTEGER NOT NULL,
    time                INTEGER NOT NULL,
    phase_order         INTEGER NOT NULL,
    kind                TEXT NOT NULL,
    action              TEXT,
    detail              TEXT,
    state_before_json   TEXT NOT NULL,
    state_after_json    TEXT NOT NULL,
    active_actions_json TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS violations (
    run_id              TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    seq                 INTEGER NOT NULL,
    category            TEXT NOT NULL,
    time                INTEGER NOT NULL,
    action              TEXT,
    resource            TEXT,
    message             TEXT NOT NULL,
    detail              TEXT,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS reference_checks (
    run_id                  TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    reference_kind          TEXT NOT NULL,
    reference_found         INTEGER NOT NULL,
    reference_makespan      INTEGER,
    solver_found            INTEGER NOT NULL,
    solver_makespan         INTEGER,
    agree                   INTEGER NOT NULL,
    schedules_evaluated     INTEGER NOT NULL,
    detail                  TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at);
CREATE INDEX IF NOT EXISTS idx_runs_fingerprint ON runs(input_fingerprint);
CREATE INDEX IF NOT EXISTS idx_violations_category ON violations(category);
"""


def connect(db_path: str | Path, *, foreign_keys: bool = True) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI serves sync endpoints from a
    # worker thread pool. Access is serialized through a single store
    # instance; WAL mode permits that safely.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
