"""测试夹具。

需要 PostgreSQL（JSONB、行锁、SKIP LOCKED 依赖 PG 语义）。
通过 TEST_DATABASE_URL 指定，默认本地 55432 端口的测试库：
    postgresql+psycopg2://flow:flow@localhost:55432/flow
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base

TEST_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg2://flow:flow@localhost:55432/flow",
)


@pytest.fixture(scope="session")
def engine_():
    eng = create_engine(TEST_URL, future=True, pool_size=10, max_overflow=20)
    # 把应用层 sessionmaker 全部指向测试库（worker 恢复路径也会用到）。
    from app.db import SessionLocal
    import app.api as api_mod

    SessionLocal.configure(bind=eng)
    api_mod.SessionLocal.configure(bind=eng)
    # 每个测试会话重建全部表，保证干净。
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture()
def db(engine_):
    """每个测试独立 Session；engine 内部会 commit，测试后 TRUNCATE 清理。"""
    from sqlalchemy import text

    Session = sessionmaker(bind=engine_, expire_on_commit=False, future=True)
    session = Session()
    yield session
    session.close()
    # 按外键依赖逆序清空
    with engine_.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE idempotency_record, escalation, history_event, task, "
                "instance, template_version, template RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture()
def rid():
    def _rid(prefix="req"):
        return f"{prefix}-{uuid.uuid4().hex}"

    return _rid


@pytest.fixture()
def simple_definition():
    """start -> all 签(alice,bob) -> condition(amount>=10000 -> any 签(carol,dave)) -> end。"""
    return {
        "start_node": "start",
        "nodes": [
            {"id": "start", "type": "start", "next": "dept"},
            {
                "id": "dept",
                "type": "approval",
                "mode": "all",
                "assignees": ["alice", "bob"],
                "next": "check",
                "timeout": {"seconds": 3600, "targets": ["frank"]},
            },
            {
                "id": "check",
                "type": "condition",
                "branches": [{"when": "amount >= 10000", "next": "gm"}],
                "default": "end",
            },
            {
                "id": "gm",
                "type": "approval",
                "mode": "any",
                "assignees": ["carol", "dave"],
                "next": "end",
            },
            {"id": "end", "type": "end"},
        ],
    }


@pytest.fixture()
def published_template(db, simple_definition):
    from app import engine

    tpl = engine.create_template(db, "leave", "请假流程", simple_definition)
    engine.publish_version(db, "leave", 1)
    return tpl
