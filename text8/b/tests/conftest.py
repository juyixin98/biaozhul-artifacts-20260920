import os

import pytest

# 默认连本地 PostgreSQL；不可用时跳过 db 测试
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg2://workflow:workflow@localhost:5432/workflow_test"
)
os.environ.setdefault("ENABLE_SWEEPER", "false")
os.environ.setdefault("SEED_DEMO_ON_START", "false")

import sqlalchemy  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from workflow_engine.database import engine  # noqa: E402
from workflow_engine.main import app  # noqa: E402
from workflow_engine.models import Base  # noqa: E402

TABLES = [
    "request_ledger",
    "audit_logs",
    "tasks",
    "node_activities",
    "instances",
    "template_versions",
    "templates",
]


def _pg_available() -> bool:
    if not engine.url.get_backend_name().startswith("postgres"):
        return False
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        return True
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    if _pg_available():
        return
    skip_db = pytest.mark.skip(reason="需要 PostgreSQL（设置 DATABASE_URL 或启动 docker compose）")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip_db)


@pytest.fixture(scope="session")
def db_schema():
    Base.metadata.create_all(engine)
    yield


@pytest.fixture(autouse=True)
def clean_db(db_schema):
    if not _pg_available():
        yield
        return
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def make_definition(**overrides):
    definition = {
        "key": "test_tpl",
        "name": "测试流程",
        "nodes": [
            {"id": "start", "type": "start", "next": "approve1"},
            {
                "id": "approve1",
                "type": "approval",
                "name": "审批1",
                "mode": "all",
                "approvers": ["alice", "bob"],
                "on_reject": "rejected_end",
                "next": "gate",
            },
            {
                "id": "gate",
                "type": "condition",
                "branches": [{"when": "amount > 100", "next": "approve2"}],
                "default": "approved_end",
            },
            {
                "id": "approve2",
                "type": "approval",
                "name": "审批2",
                "mode": "any",
                "approvers": ["carol", "dave"],
                "on_reject": "rejected_end",
                "next": "approved_end",
            },
            {"id": "approved_end", "type": "end", "outcome": "approved"},
            {"id": "rejected_end", "type": "end", "outcome": "rejected"},
        ],
    }
    definition.update(overrides)
    return definition


@pytest.fixture
def published_template(client):
    def _publish(definition=None, key="test_tpl"):
        definition = definition or make_definition(key=key)
        r = client.post("/templates", json={"definition": definition, "publish": True})
        assert r.status_code == 201, r.text
        return r.json(), definition

    return _publish


@pytest.fixture
def start_instance(client, published_template):
    published_keys: set[str] = set()

    def _start(context=None, *, user="tom", key="test_tpl", version=None, title="测试单"):
        if key not in published_keys:
            published_template(key=key)
            published_keys.add(key)
        body = {"template_key": key, "title": title, "context": context or {}}
        if version is not None:
            body["version"] = version
        r = client.post("/instances", json=body, headers={"X-User": user})
        assert r.status_code == 201, r.text
        return r.json()

    return _start
