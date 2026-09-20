"""Database engine and session management."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def get_session() -> Session:
    """FastAPI dependency: one transactional session per request."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
