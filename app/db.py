"""SQLite 持久层。

核心表 (bags/params/calibrations/algorithms/snapshots/runs/index_entries)
通过触发器禁止 UPDATE/DELETE: 实验快照一旦写入即不可变。

attempts 表需要状态机推进 (running -> succeeded/failed/interrupted 以及
marked_reproducible), 应用层只允许修改这几个状态/时间列, 记录输入与种子
等身份列在触发器中保持不可变。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS bags (
    id          TEXT PRIMARY KEY,
    sha256      TEXT NOT NULL UNIQUE,
    size_bytes  INTEGER NOT NULL,
    summary     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS params (
    id          TEXT PRIMARY KEY,
    body_json   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calibrations (
    id          TEXT PRIMARY KEY,
    body_json   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS algorithms (
    id          TEXT PRIMARY KEY,
    body_json   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id              TEXT PRIMARY KEY,
    bag_id          TEXT NOT NULL REFERENCES bags(id),
    params_id       TEXT NOT NULL REFERENCES params(id),
    calibration_id  TEXT NOT NULL REFERENCES calibrations(id),
    algorithm_id    TEXT NOT NULL REFERENCES algorithms(id),
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    snapshot_id  TEXT NOT NULL REFERENCES snapshots(id),
    seed         INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE(snapshot_id, seed)
);

CREATE TABLE IF NOT EXISTS attempts (
    id                 TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL REFERENCES runs(id),
    attempt_no         INTEGER NOT NULL,
    status             TEXT NOT NULL CHECK(status IN
                         ('running','succeeded','failed','interrupted',
                          'marked_reproducible')),
    error_code         TEXT,
    error_message      TEXT,
    result_artifact_id TEXT,
    error_artifact_id  TEXT,
    index_entry_id     TEXT,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    UNIQUE(run_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id           TEXT PRIMARY KEY,
    sha256       TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL CHECK(kind IN ('result','error')),
    size_bytes   INTEGER NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_entries (
    id           TEXT PRIMARY KEY,
    attempt_id   TEXT NOT NULL UNIQUE REFERENCES attempts(id),
    payload_json TEXT NOT NULL,
    signature    TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""

# 完全不可变的表: 任何 UPDATE/DELETE 都被数据库本身拒绝。
_IMMUTABLE_TABLES = (
    "bags",
    "params",
    "calibrations",
    "algorithms",
    "snapshots",
    "runs",
    "artifacts",
    "index_entries",
)

_TRIGGERS = []
for _t in _IMMUTABLE_TABLES:
    _TRIGGERS.append(f"""
CREATE TRIGGER IF NOT EXISTS {_t}_no_update BEFORE UPDATE ON {_t}
BEGIN
    SELECT RAISE(ABORT, 'table {_t} is immutable');
END;
""")
    _TRIGGERS.append(f"""
CREATE TRIGGER IF NOT EXISTS {_t}_no_delete BEFORE DELETE ON {_t}
BEGIN
    SELECT RAISE(ABORT, 'table {_t} is immutable');
END;
""")

# attempts: 身份列不可变; 只允许应用层推进状态机相关列。
_TRIGGERS.append("""
CREATE TRIGGER IF NOT EXISTS attempts_identity_no_update BEFORE UPDATE ON attempts
WHEN NEW.id IS NOT OLD.id
  OR NEW.run_id IS NOT OLD.run_id
  OR NEW.attempt_no IS NOT OLD.attempt_no
  OR NEW.started_at IS NOT OLD.started_at
BEGIN
    SELECT RAISE(ABORT, 'attempt identity columns are immutable');
END;
""")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for ddl in _TRIGGERS:
        conn.executescript(ddl)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
