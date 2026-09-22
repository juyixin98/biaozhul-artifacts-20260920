"""手写 SQL 迁移运行器：每个 .sql 文件在独立事务中执行一次。"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .config import config
from .db import _sqlite_path  # noqa: PLC2701  内部工具复用


def _connect() -> sqlite3.Connection:
    path = _sqlite_path(config.db_url)
    parent = os.path.dirname(path)
    if parent and parent not in (":memory:", ""):
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def run_migrations() -> list[str]:
    migrations_dir = Path(config.migrations_dir)
    files = sorted(p for p in migrations_dir.glob("*.sql") if p.is_file())
    conn = _connect()
    applied: list[str] = []
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name VARCHAR(200) PRIMARY KEY, applied_at DATETIME DEFAULT (datetime('now')))"
        )
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        for path in files:
            if path.name in done:
                continue
            sql = path.read_text(encoding="utf-8")
            # DDL 全部使用 IF NOT EXISTS，天然可重入；崩溃在中途时重跑安全。
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations(name) VALUES (?)", (path.name,))
            applied.append(path.name)
    finally:
        conn.close()
    return applied
