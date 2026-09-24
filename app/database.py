"""SQLite 持久层：schema、连接管理与小工具。

状态约定：
- allocation.status: planned（已预占、未开始）/ running（执行中）/
                     completed / cancelled
- charger.status:    available / failed
- 同一任务同一时刻最多一条活跃分配（部分唯一索引强制，防重复接单）。
- 每个可用充电点同一时刻最多被一条活跃分配预占（部分唯一索引强制竞争）。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS robots (
    id                  TEXT PRIMARY KEY,
    x                   REAL NOT NULL,
    y                   REAL NOT NULL,
    battery_wh          REAL NOT NULL,           -- 最近一次测得的电量
    status              TEXT NOT NULL DEFAULT 'idle',  -- idle / busy
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chargers (
    id          TEXT PRIMARY KEY,
    x           REAL NOT NULL,
    y           REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'available', -- available / failed
    failed_at   TEXT,
    reason      TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    x               REAL NOT NULL,
    y               REAL NOT NULL,
    payload_kg      REAL NOT NULL,
    wait_s          REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending / assigned /
                                                     -- running / completed /
                                                     -- cancelled
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS allocations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL REFERENCES tasks(id),
    robot_id            TEXT NOT NULL REFERENCES robots(id),
    charger_id          TEXT REFERENCES chargers(id),  -- 预占的返航充电点
    distance_out_m      REAL NOT NULL,
    distance_return_m   REAL NOT NULL,
    outbound_energy_wh  REAL NOT NULL,
    wait_energy_wh      REAL NOT NULL,
    return_energy_wh    REAL NOT NULL,
    total_energy_wh     REAL NOT NULL,
    safety_margin_wh    REAL NOT NULL,
    reserved_energy_wh  REAL NOT NULL,  -- total + margin，写入时预占
    status              TEXT NOT NULL DEFAULT 'planned',
    start_battery_wh    REAL,            -- 开始执行时实测电量快照
    start_at            TEXT,
    complete_at         TEXT,
    created_at          TEXT NOT NULL,
    completed_at        TEXT,
    cancelled_at        TEXT
);

-- 同一任务只允许一条活跃（planned/running）分配 —— 防重复接单
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_allocation_per_task
    ON allocations(task_id)
    WHERE status IN ('planned', 'running');

-- 同一机器人只允许一条活跃分配
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_allocation_per_robot
    ON allocations(robot_id)
    WHERE status IN ('planned', 'running');

-- 可用充电点只能被一条活跃分配预占 —— 竞争在数据库层强制
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_claim_per_charger
    ON allocations(charger_id)
    WHERE status IN ('planned', 'running');

CREATE TABLE IF NOT EXISTS telemetry (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    robot_id                TEXT NOT NULL REFERENCES robots(id),
    battery_wh              REAL NOT NULL,
    x                       REAL,
    y                       REAL,
    predicted_remaining_wh  REAL,
    recorded_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,            -- 见 ALERT_KIND_*
    severity    TEXT NOT NULL,            -- warning / critical
    robot_id    TEXT,
    task_id     TEXT,
    charger_id  TEXT,
    message     TEXT NOT NULL,
    data_json   TEXT,
    created_at  TEXT NOT NULL
);

-- 签名随机数（防重放）
CREATE TABLE IF NOT EXISTS used_nonces (
    nonce       TEXT PRIMARY KEY,
    ts          INTEGER NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path or config.DB_PATH,
        timeout=30.0,
        isolation_level=None,  # 手动事务，BEGIN IMMEDIATE
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每个请求一个连接。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def immediate_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即获取写锁的事务，保证「分配 + 电量预占 + 充电点预占」原子提交。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
