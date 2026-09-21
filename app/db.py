"""Database engine / session setup."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        # Needed for SELECT ... FOR UPDATE tests sharing one file connection.
        connect_args = {"timeout": 30, "check_same_thread": False}
    engine = create_engine(url, future=True, connect_args=connect_args, pool_pre_ping=True)
    return engine


engine: Engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a transactional session."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
