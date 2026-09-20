"""Shared pytest fixtures.

* Creates an isolated test database (DATABASE_URL, e.g. cloudgate_test) once,
  migrates it with Alembic and truncates every table before each test.
* An httpx-backed FastAPI TestClient (real thread pool => real concurrency).
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.bootstrap import seed_platform_admin
from app.config import settings


def _admin_engine(url: str):
    parsed = make_url(url)
    admin_url = parsed.set(database="postgres")
    return create_engine(admin_url.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT")


@pytest.fixture(scope="session", autouse=True)
def _prepare_database():
    url = make_url(settings.database_url)
    db_name = url.database
    assert db_name, "DATABASE_URL must name a database"
    if db_name == "cloudgate":
        # Guard against clobbering a dev database when tests run locally.
        if os.environ.get("ALLOW_TESTS_ON_MAIN_DB") != "1":
            raise RuntimeError(
                f"tests target database '{db_name}'; point DATABASE_URL at a *_test database "
                "or set ALLOW_TESTS_ON_MAIN_DB=1"
            )

    engine = _admin_engine(settings.database_url)
    with engine.connect() as conn:
        exists = conn.execute(
            text("select 1 from pg_database where datname = :name"), {"name": db_name}
        ).scalar()
        if not exists:
            conn.execute(text(f'create database "{db_name}"'))
    engine.dispose()

    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    config.set_main_option("script_location", "migrations")
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")
    yield


@pytest.fixture(autouse=True)
def _clean_database(_prepare_database):
    from app.db import engine

    with engine.begin() as conn:
        conn.execute(text("truncate table lease_terminations, sessions, devices, access_points,"
                          " pool_addresses, address_pools, admin_users, tenants restart identity cascade"))
    from app.db import SessionLocal

    with SessionLocal() as db:
        seed_platform_admin(db)
    yield
    # Dispose pooled connections so connection state never leaks between tests.
    engine.dispose()


@pytest.fixture
def client():
    from app.main import create_app

    app = create_app(auto_migrate=False, auto_seed=False)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

PLATFORM = {"username": settings.platform_admin_username, "password": settings.platform_admin_password}


@pytest.fixture
def platform_token(client) -> str:
    resp = client.post("/api/admin/login", json=PLATFORM)
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest.fixture
def make_tenant(client, platform_token):
    def _make(name: str | None = None):
        name = name or f"tenant-{uuid.uuid4().hex[:8]}"
        resp = client.post("/api/platform/tenants", headers=_auth(platform_token), json={"name": name})
        assert resp.status_code == 201, resp.text
        tenant_id = resp.json()["id"]
        username = f"admin-{uuid.uuid4().hex[:10]}"
        password = "tenant-pass-1234"
        resp = client.post(
            f"/api/platform/tenants/{tenant_id}/admins",
            headers=_auth(platform_token),
            json={"username": username, "password": password},
        )
        assert resp.status_code == 201, resp.text
        resp = client.post("/api/admin/login", json={"username": username, "password": password})
        assert resp.status_code == 200, resp.text
        return {
            "id": tenant_id,
            "name": name,
            "token": resp.json()["access_token"],
            "base": f"/api/tenants/{tenant_id}",
        }

    return _make


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def make_pool(client, make_tenant):
    def _make(tenant=None, *, cidr="10.10.0.0/24", reserved=None, name=None):
        tenant = tenant or make_tenant()
        name = name or f"pool-{uuid.uuid4().hex[:8]}"
        resp = client.post(
            f"{tenant['base']}/pools",
            headers=_auth(tenant["token"]),
            json={"name": name, "cidr": cidr, "reserved_addresses": reserved or []},
        )
        assert resp.status_code == 201, resp.text
        pool = resp.json()
        pool["tenant"] = tenant
        return pool

    return _make


@pytest.fixture
def make_ap(client, make_pool):
    def _make(pool=None, *, capacity=10, cidr="10.10.0.0/24", reserved=None, name=None):
        pool = pool or make_pool(cidr=cidr, reserved=reserved)
        tenant = pool["tenant"]
        name = name or f"ap-{uuid.uuid4().hex[:8]}"
        resp = client.post(
            f"{tenant['base']}/access-points",
            headers=_auth(tenant["token"]),
            json={"name": name, "pool_id": pool["id"], "capacity": capacity},
        )
        assert resp.status_code == 201, resp.text
        ap = resp.json()
        ap["tenant"] = tenant
        ap["pool"] = pool
        return ap

    return _make


@pytest.fixture
def make_device(client):
    def _make(tenant, *, name=None):
        name = name or f"dev-{uuid.uuid4().hex[:8]}"
        resp = client.post(
            f"{tenant['base']}/devices",
            headers=_auth(tenant["token"]),
            json={"name": name},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        return {"id": body["id"], "token": body["device_token"], "tenant": tenant, "name": body["name"]}

    return _make


@pytest.fixture
def api_connect(client):
    def _connect(device, ap, *, key=None):
        payload = {"access_point_id": ap["id"], "idempotency_key": key if key is not None else str(uuid.uuid4())}
        return client.post(
            "/api/device/connect",
            headers={"X-Device-Token": device["token"]},
            json=payload,
        )

    return _connect

