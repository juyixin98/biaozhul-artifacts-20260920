from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from workflow_engine.config import get_settings


def make_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    connect_args: dict = {}
    if url.startswith("sqlite"):
        # 测试用：SQLite 需要同线程检查关闭
        connect_args["check_same_thread"] = False
    return create_engine(url, pool_pre_ping=True, future=True, connect_args=connect_args)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_session() -> Iterator[Session]:
    """FastAPI 依赖：提供一个 Session（路由内自行管理事务）。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
