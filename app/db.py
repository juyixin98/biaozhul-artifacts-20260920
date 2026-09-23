"""数据库引擎、会话工厂与建表。"""
from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from .config import get_settings
from .models import Base

_engine = None
_SessionLocal = None


def _make_engine(url: str):
    return create_engine(url, pool_pre_ping=True, future=True)


def init_engine(url: str | None = None):
    global _engine, _SessionLocal
    if url is None:
        url = os.environ.get("DATABASE_URL") or get_settings().database_url
    _engine = _make_engine(url)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, future=True)
    return _engine


def get_engine():
    if _engine is None:
        init_engine()
    return _engine


def create_all():
    Base.metadata.create_all(get_engine())


def drop_all():
    Base.metadata.drop_all(get_engine())


def db_session() -> Session:
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def get_db():
    """FastAPI 依赖：每请求一个会话。"""
    db = db_session()
    try:
        yield db
    finally:
        db.close()
