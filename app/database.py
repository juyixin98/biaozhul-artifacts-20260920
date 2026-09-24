"""SQLite connection management and schema creation."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS operators (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS robots (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    pos_x              REAL NOT NULL,
    pos_y              REAL NOT NULL,
    current_soc_kwh    REAL NOT NULL,
    capacity_kwh       REAL NOT NULL,
    payload_capacity_kg REAL NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'idle'
        CHECK (status IN ('idle','reserved','running','charging','done')),
    at_risk            INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chargers (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    pos_x         REAL NOT NULL,
    pos_y         REAL NOT NULL,
    capacity      INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'available'
        CHECK (status IN ('available','failed')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    pickup_x     REAL NOT NULL,
    pickup_y     REAL NOT NULL,
    delivery_x   REAL NOT NULL,
    delivery_y   REAL NOT NULL,
    payload_kg   REAL NOT NULL,
    wait_seconds REAL NOT NULL DEFAULT 0,
    priority     INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','assigned','running','done','cancelled')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS assignments (
    id                 TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL REFERENCES tasks(id),
    robot_id           TEXT NOT NULL REFERENCES robots(id),
    charger_id         TEXT NOT NULL REFERENCES chargers(id),
    status             TEXT NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved','running','charging','done','cancelled')),
    token              TEXT NOT NULL,
    explanation_json   TEXT NOT NULL,
    predicted_soc_kwh  REAL NOT NULL,   -- SoC predicted at dispatch time
    required_soc_kwh   REAL NOT NULL,   -- budget incl. safety margin
    reserve_eta_m      REAL NOT NULL,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    started_at         TEXT,
    completed_at       TEXT
);

-- At most one *active* assignment per robot and one per task.
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_robot ON assignments(robot_id)
    WHERE status IN ('reserved','running','charging');
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_task ON assignments(task_id)
    WHERE status IN ('reserved','running','charging');

CREATE TABLE IF NOT EXISTS telemetry (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id     TEXT NOT NULL REFERENCES assignments(id),
    measured_soc_kwh  REAL NOT NULL,
    predicted_soc_kwh REAL NOT NULL,
    deviation_ratio   REAL NOT NULL,
    severity          TEXT NOT NULL CHECK (severity IN ('ok','warning','critical')),
    note              TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL,          -- battery_risk|charger_failure|...
    severity       TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    entity_type    TEXT NOT NULL,          -- robot|assignment|charger|task
    entity_id      TEXT NOT NULL,
    message        TEXT NOT NULL,
    detail_json    TEXT,
    acknowledged   INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dispatch_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id TEXT,
    action        TEXT NOT NULL,           -- dispatch_simulated|start|...
    payload_json  TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(db_path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path or config.DB_PATH), timeout=30, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path=None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit BEGIN/COMMIT with rollback on error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
