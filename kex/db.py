"""SQLAlchemy engine/session factory and SQLite pragmas.

SQLite runs in WAL mode with a busy timeout so that the web process and up to
two worker processes can share the database file safely. Every connection
enforces foreign keys (PRAGMA foreign_keys is per-connection in SQLite).

Concurrency model
-----------------
SQLite serializes writers even in WAL mode. Two defenses are combined:

1. :func:`write_session` holds a process-local re-entrant lock for the whole
   read-modify-write transaction, so the two in-process worker threads never
   interleave (and never invert Python-lock vs SQLite-lock ordering).
2. Write sessions issue ``BEGIN IMMEDIATE``, acquiring the RESERVED lock at
   transaction start. Across OS processes (web + worker containers) writers
   therefore queue fairly instead of deadlocking when two deferred
   SHARED->RESERVED upgrades collide; the rare remaining BUSY/UNIQUE race is
   handled by item-level optimistic retry in the worker.

Read-only web requests use ordinary deferred transactions and never take the
write lock; heavy worker writes use :func:`write_session`.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Config, load_config

_config: Config | None = None
_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None
_WriteSessionLocal: sessionmaker[Session] | None = None

# Process-local writer serialization (see module docstring).
_write_lock = threading.RLock()


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def configure(config: Config) -> None:
    """Override the global configuration (used by tests)."""
    global _config, _engine, _SessionLocal, _WriteSessionLocal
    _config = config
    _engine = None
    _SessionLocal = None
    _WriteSessionLocal = None


def get_engine() -> Engine:
    global _engine, _SessionLocal, _WriteSessionLocal
    if _engine is None:
        cfg = get_config()
        # check_same_thread=False: SQLAlchemy serializes access per Session;
        # workers open their own short-lived Sessions/connections.
        _engine = create_engine(
            cfg.sqlalchemy_url,
            echo=cfg.echo_sql,
            future=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            # WAL: multi-reader / single-writer across processes, survives
            # crashes without corrupting the database.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA synchronous=NORMAL")
            # Wait up to 30s instead of failing immediately on SQLITE_BUSY.
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        @event.listens_for(_engine, "begin")
        def _on_begin(conn):  # noqa: ANN001
            # Write sessions are created with this execution option. Taking
            # RESERVED up front makes cross-process writers queue cleanly
            # instead of deadlocking on deferred SHARED->RESERVED upgrades.
            if conn._execution_options.get("sqlite_begin_immediate"):
                conn.exec_driver_sql("BEGIN IMMEDIATE")

        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
        _WriteSessionLocal = sessionmaker(
            bind=_engine.execution_options(sqlite_begin_immediate=True),
            expire_on_commit=False,
            future=True,
            class_=Session,
        )
    return _engine


def get_write_sessionmaker() -> sessionmaker[Session]:
    get_engine()
    assert _WriteSessionLocal is not None
    return _WriteSessionLocal


def get_sessionmaker() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Plain transactional session scope.

    Read-only callers can use this directly; write transactions MUST go
    through :func:`write_session` (or hold :func:`write_lock` for the whole
    transaction lifetime). Acquiring the Python write lock only at commit
    time would deadlock against another connection that already holds
    SQLite's RESERVED lock.
    """
    SessionLocal = get_sessionmaker()
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def write_lock() -> Iterator[None]:
    """Process-local serialization of all SQLite write transactions.

    MUST be held for the whole read-modify-write transaction (including any
    intermediate checkpoint commits), not just the final commit. This makes
    lock acquisition order identical on every thread and rules out
    Python-lock / SQLite-lock inversion deadlocks.
    """
    with _write_lock:
        yield


@contextmanager
def write_session() -> Iterator[Session]:
    """Write transaction: Python lock held + BEGIN IMMEDIATE at start."""
    WriteSessionLocal = get_write_sessionmaker()
    db = WriteSessionLocal()
    with _write_lock:
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
