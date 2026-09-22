"""PostgreSQL 连接池与 schema 初始化 (psycopg v3)。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

from .config import DATABASE_URL

_pool: ConnectionPool | None = None
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def init_pool(database_url: str | None = None, *, min_size: int = 1, max_size: int = 10) -> ConnectionPool:
    global _pool
    if _pool is not None and not _pool.closed:
        return _pool
    _pool = ConnectionPool(
        database_url or DATABASE_URL,
        min_size=min_size,
        max_size=max_size,
        kwargs={"row_factory": dict_row},
        open=True,
    )
    return _pool


def get_pool() -> ConnectionPool:
    if _pool is None or _pool.closed:
        init_pool()
    assert _pool is not None
    return _pool


def init_schema() -> None:
    ddl = _SCHEMA_PATH.read_text(encoding="utf-8")
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()


def close_pool() -> None:
    global _pool
    if _pool is not None and not _pool.closed:
        _pool.close()
    _pool = None


def fetchone_dict(cur: psycopg.Cursor[Any]) -> dict[str, Any] | None:
    row = cur.fetchone()
    return dict(row) if row is not None else None
