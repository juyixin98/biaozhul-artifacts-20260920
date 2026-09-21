"""SQLAlchemy engine, session factory and declarative base."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import CHECKPOINT_DIR, database_url


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    return create_engine(
        url or database_url(),
        pool_pre_ping=True,
        future=True,
    )


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create tables and local directories. Imported models register metadata."""
    from . import models  # noqa: F401  (ensure mappers are configured)

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
