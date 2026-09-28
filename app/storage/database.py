"""SQLite 连接管理。

每个操作从连接池语义中取出独立连接（SQLite 连接不可跨线程共享），
写事务一律 ``BEGIN IMMEDIATE``：在 WAL 下写者串行排队，配合
``busy_timeout``，并发登记表现为"排队提交"而不是偶发失败。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .schema import SCHEMA_SQL, SCHEMA_VERSION


class Database:
    def __init__(self, path: str, *, busy_timeout_ms: int = 10_000):
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        else:
            # 单连接共享内存库：仅供不需要真实并发的单元测试使用。
            # 并发/恢复测试必须使用文件库（WAL + busy_timeout）。
            self._shared_conn = self._connect()
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,  # 手动事务
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys=ON")
        if self.path == ":memory:":
            conn.execute("PRAGMA journal_mode=MEMORY")
        else:
            conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if self.path == ":memory:":
            yield self._shared_conn  # 不关闭：全库共享
            return
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write_tx(self) -> Iterator[sqlite3.Connection]:
        """串行写事务：BEGIN IMMEDIATE 立即拿写锁。"""
        if self.path == ":memory:":
            conn = self._shared_conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.write_tx() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (str(SCHEMA_VERSION),),
            )
