"""测试夹具。

所有测试运行在独立的 cloudgate_test 数据库上：
- 通过 Alembic 迁移建表（同时验证迁移脚本可用）；
- 每个用例前 TRUNCATE 全部业务表；
- 关闭后台回收线程（REAPER_INTERVAL=0），过期回收在测试中显式触发，
  以保证“重启恢复”等场景可控。
"""
from __future__ import annotations

import os

os.environ.setdefault(
    "CLOUDGATE_DATABASE_URL",
    "postgresql+psycopg2://cloudgate:cloudgate@localhost:5432/cloudgate_test",
)
os.environ.setdefault("CLOUDGATE_SUPER_ADMIN_KEY", "test-super-key")
os.environ.setdefault("CLOUDGATE_REAPER_INTERVAL_SECONDS", "0")
os.environ.setdefault("CLOUDGATE_HEARTBEAT_TIMEOUT_SECONDS", "600")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _prepare_database():
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def _truncate_tables():
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE TABLE lease_events, leases, devices, address_pools, "
            "access_points, tenants RESTART IDENTITY CASCADE"
        ))
    yield


@pytest.fixture
def client():
    # reaper 间隔为 0：lifespan 只执行一次启动回收，不起后台循环
    with TestClient(app) as c:
        yield c


SUPER_HEADERS = {"X-Admin-Key": "test-super-key"}


class Tenant:
    def __init__(self, client: TestClient, name: str = "acme", headers: dict | None = None,
                 info: dict | None = None):
        self.client = client
        self.name = name
        self.headers = headers or {}
        self.info = info or {}


@pytest.fixture
def make_tenant(client):
    """工厂：创建租户，返回带管理员鉴权头的 Tenant 对象。"""
    counter = {"n": 0}

    def _make(name: str | None = None):
        counter["n"] += 1
        tname = name or f"tenant-{counter['n']}"
        r = client.post("/admin/tenants", json={"name": tname}, headers=SUPER_HEADERS)
        assert r.status_code == 201, r.text
        info = r.json()
        return Tenant(client, tname, {"X-Admin-Key": info["admin_key"]}, info)

    return _make


@pytest.fixture
def make_ap(client):
    def _make(tenant, name="ap-1", capacity=100):
        r = client.post("/access-points",
                        json={"name": name, "capacity": capacity}, headers=tenant.headers)
        assert r.status_code == 201, r.text
        return r.json()
    return _make


@pytest.fixture
def make_pool(client):
    def _make(tenant, ap_id, cidr="10.10.0.0/24", reserved_first=0, reserved_last=0):
        r = client.post(f"/access-points/{ap_id}/pools",
                        json={"cidr": cidr, "reserved_first": reserved_first,
                              "reserved_last": reserved_last},
                        headers=tenant.headers)
        assert r.status_code == 201, r.text
        return r.json()
    return _make


@pytest.fixture
def make_device(client):
    def _make(tenant, name="dev-1"):
        r = client.post("/devices", json={"name": name}, headers=tenant.headers)
        assert r.status_code == 201, r.text
        body = r.json()
        body["token_headers"] = {"X-Device-Token": body["token"]}
        return body
    return _make


@pytest.fixture
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
