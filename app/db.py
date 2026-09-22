"""数据库引擎与会话。

SQLite 配置要点：
- WAL：多读单写，读写不互斥（写之间仍串行）。
- 所有写事务走 BEGIN IMMEDIATE（``immediate_transaction``），抢不到写锁时按
  busy_timeout 等待，杜绝先读后写的升级型死锁。
- foreign_keys=ON；synchronous=NORMAL 在 WAL 下兼顾安全与速度。

引擎为惰性单例，测试可通过 ``reset_engine`` 换库。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, scoped_session, sessionmaker

from .config import config

_BUSY_TIMEOUT_MS = 10_000

# Python 3.12 弃用了默认 datetime 适配器，显式注册 ISO8601（UTC，微秒）。
def _adapt_datetime(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(sep=" ", timespec="microseconds")


sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_adapter(date, lambda value: value.isoformat())

_engine: Engine | None = None
_SessionLocal: scoped_session | None = None
_engine_lock = threading.Lock()
# immediate_transaction 进程内互斥：同进程内写关键区串行，配合 DB 写锁跨进程互斥。
_immediate_lock = threading.RLock()


def _build_engine() -> Engine:
    connect_args = {"timeout": _BUSY_TIMEOUT_MS / 1000.0, "check_same_thread": False}
    eng = create_engine(config.db_url, future=True, connect_args=connect_args)

    if config.db_url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn: sqlite3.Connection, _record) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=10000")
            cur.execute("PRAGMA wal_autocheckpoint=1000")
            cur.close()

    return eng


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = _build_engine()
                _SessionLocal = scoped_session(
                    sessionmaker(bind=_engine, future=True, expire_on_commit=False)
                )
    return _engine


def reset_engine() -> None:
    """丢弃当前引擎与会话工厂（测试换库用）。"""
    global _engine, _SessionLocal
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _SessionLocal = None


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _SessionLocal is not None
    sess = _SessionLocal()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        _SessionLocal.remove()


def _sqlite_path(db_url: str) -> str:
    """sqlite:////abs/path -> /abs/path ; sqlite:///rel/path -> rel/path"""
    if db_url.startswith("sqlite:////"):
        return "/" + db_url[len("sqlite:////"):]
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///"):]
    return db_url[len("sqlite://"):]


@contextmanager
def immediate_transaction() -> Iterator[sqlite3.Connection]:
    """进程内互斥 + BEGIN IMMEDIATE 的关键区写事务。

    用于作业认领、索引代次切换等必须串行且读-判-写原子的路径。
    返回底层 sqlite3 连接，with 退出时 COMMIT；异常 ROLLBACK。
    """
    if not config.db_url.startswith("sqlite"):  # 非 sqlite 兜底
        eng = get_engine()
        with eng.begin() as conn:
            yield conn.connection.dbapi_connection
        return

    path = _sqlite_path(config.db_url)
    parent = os.path.dirname(path)
    if parent and parent not in ("", ":memory:"):
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_MS / 1000.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        with _immediate_lock:
            deadline = time.monotonic() + 15.0
            while True:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError:  # database is locked
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.05)
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()
