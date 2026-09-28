"""SQLite schema and connection management.

The database is the durable *evidence* store: problem definitions (DSL
source plus parsed entities) and propagation snapshots (labels, nogoods,
run counters).  It stores no secrets; every deployment runs against a
local file with synthetic data.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS problems (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    source_dsl  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS assumptions (
    problem_id TEXT NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
    node_id    TEXT NOT NULL,
    ord        INTEGER NOT NULL,
    PRIMARY KEY (problem_id, node_id)
);

CREATE TABLE IF NOT EXISTS facts (
    problem_id TEXT NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
    node_id    TEXT NOT NULL,
    ord        INTEGER NOT NULL,
    PRIMARY KEY (problem_id, node_id)
);

CREATE TABLE IF NOT EXISTS rules (
    problem_id  TEXT NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
    rule_id     TEXT NOT NULL,
    antecedents TEXT NOT NULL,  -- JSON array of node ids, in order
    consequent  TEXT NOT NULL,
    ord         INTEGER NOT NULL,
    PRIMARY KEY (problem_id, rule_id)
);

CREATE TABLE IF NOT EXISTS label_snapshots (
    problem_id TEXT NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
    node_id    TEXT NOT NULL,
    env_json   TEXT NOT NULL,   -- JSON array of assumption ids
    ord        INTEGER NOT NULL,
    PRIMARY KEY (problem_id, node_id, env_json)
);

CREATE TABLE IF NOT EXISTS nogood_snapshots (
    problem_id TEXT NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
    env_json   TEXT NOT NULL,
    ord        INTEGER NOT NULL,
    PRIMARY KEY (problem_id, env_json)
);

CREATE TABLE IF NOT EXISTS propagation_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id  TEXT NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
    request_id  TEXT NOT NULL,
    incomplete  INTEGER NOT NULL,
    reason      TEXT,
    steps       INTEGER NOT NULL,
    total_envs  INTEGER NOT NULL,
    new_nogoods INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_runs_problem ON propagation_runs(problem_id, id);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with the pragmas the repository relies on."""
    path = str(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    # Teaching deployment: a single shared connection serialized by SQLite's
    # own locking; WAL keeps readers/writers from blocking each other.
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    """Create tables and record the schema version (idempotent)."""
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
