"""SQLite connection management and DDL."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corpora (
    corpus_id   TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    spec_json   TEXT NOT NULL,
    loaded_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transducers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    corpus_id   TEXT NOT NULL REFERENCES corpora(corpus_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('fst', 'lexicon', 'composed')),
    num_states  INTEGER NOT NULL,
    num_arcs    INTEGER NOT NULL,
    fst_json    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (corpus_id, name)
);

CREATE TABLE IF NOT EXISTS pipelines (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    corpus_id     TEXT NOT NULL REFERENCES corpora(corpus_id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    sequence_json TEXT NOT NULL,
    composed_name TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (corpus_id, name)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    corpus_id    TEXT NOT NULL REFERENCES corpora(corpus_id),
    target       TEXT NOT NULL,
    input        TEXT NOT NULL,
    k            INTEGER NOT NULL,
    budget       INTEGER NOT NULL,
    status       TEXT NOT NULL,
    error_code   TEXT,
    error_message TEXT,
    complete     INTEGER NOT NULL DEFAULT 0,
    expansions   INTEGER NOT NULL DEFAULT 0,
    request_json TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT
);

CREATE TABLE IF NOT EXISTS results (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    rank    INTEGER NOT NULL,
    output  TEXT NOT NULL,
    cost    REAL NOT NULL,
    UNIQUE (run_id, rank)
);

CREATE TABLE IF NOT EXISTS run_logs (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    seq    INTEGER NOT NULL,
    line   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transducers_corpus ON transducers(corpus_id);
CREATE INDEX IF NOT EXISTS idx_runs_corpus ON runs(corpus_id);
CREATE INDEX IF NOT EXISTS idx_run_logs_run ON run_logs(run_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite database with the pragmas the service relies on.

    ``check_same_thread=False`` is required because FastAPI executes sync
    endpoints in a worker thread different from the one that constructed
    the connection.  A single shared connection guarded by SQLite's own
    locking plus ``busy_timeout`` is sufficient for this single-process
    local service.
    """
    conn = sqlite3.connect(
        str(path), check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
