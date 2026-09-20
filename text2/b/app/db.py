"""Database engine, session factory and ORM base.

A single engine is created lazily from :data:`app.config.settings`. Tests
override the URL through ``configure_engine`` (see ``tests/conftest.py``).
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def configure_engine(database_url: str | None = None) -> Engine:
    """(Re)create the global engine and session factory.

    Called once at application startup and by tests that point at a dedicated
    database. ``pool_pre_ping`` avoids stale connections after a DB restart.
    """
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    url = database_url or settings.database_url
    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        # Needed for SELECT ... FOR UPDATE skip-locking emulation in tests.
        connect_args["timeout"] = 30
    _engine = create_engine(
        url,
        pool_pre_ping=True,
        future=True,
        connect_args=connect_args,
    )
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        configure_engine()
    assert _engine is not None
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _SessionLocal is None:
        configure_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    factory = get_session_factory()
    db = factory()
    try:
        yield db
    finally:
        db.close()
