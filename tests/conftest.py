import asyncio
import os
import secrets
import time

os.environ.setdefault("CLOUDGATE_DATABASE_URL", "postgresql+psycopg2://cloudgate:cloudgate@127.0.0.1:5432/cloudgate_test")
os.environ.setdefault("CLOUDGATE_SUPER_ADMIN_KEY", "test-super-key")
os.environ.setdefault("CLOUDGATE_TOKEN_SECRET", "test-token-secret")
os.environ.setdefault("CLOUDGATE_RUN_SWEEPER", "false")

import anyio
import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import get_settings
from app.db import SessionLocal, engine
from app.main import create_app

TABLES = [
    "lease_events",
    "leases",
    "devices",
    "access_points",
    "address_pools",
    "tenants",
]


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """The schema is created via Alembic migrations — migrations are part of the product."""
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


@pytest.fixture(autouse=True)
def _clean_tables():
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture(autouse=True)
def _short_ttl(monkeypatch):
    # Fast expiry for the recovery/sweeper tests without sleeping 10 minutes.
    settings = get_settings()
    monkeypatch.setattr(settings, "heartbeat_ttl_seconds", 3)
    monkeypatch.setattr(settings, "sweeper_interval_seconds", 0.2)
    monkeypatch.setattr(settings, "run_sweeper", False)


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


class AsyncHarness:
    """Runs an async test body on a fresh event loop with an ASGI client.

    Concurrent tests use this instead of threads + TestClient: requests are
    coroutines scheduled concurrently, while the app's synchronous DB work runs
    in the to_thread pool (tokens raised so a burst cannot starve itself).
    The ASGI lifespan (startup recovery + shutdown) is driven on the same loop.
    """

    def __init__(self):
        self.app = create_app()

    def run(self, coro_factory):
        return asyncio.run(self._amain(coro_factory))

    async def _amain(self, coro_factory):
        anyio.to_thread.current_default_thread_limiter().total_tokens = 200

        # Drive the ASGI lifespan on this same loop.
        inbox: asyncio.Queue = asyncio.Queue()
        outbox: asyncio.Queue = asyncio.Queue()

        async def receive():
            return await inbox.get()

        async def send(message):
            await outbox.put(message)

        lifespan = asyncio.create_task(self.app({"type": "lifespan"}, receive, send))
        await inbox.put({"type": "lifespan.startup"})
        started = await outbox.get()
        if started["type"] != "lifespan.startup.complete":
            raise RuntimeError(f"app startup failed: {started}")

        transport = httpx.ASGITransport(app=self.app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                return await coro_factory(ac)
        finally:
            await inbox.put({"type": "lifespan.shutdown"})
            stopping = await outbox.get()
            if stopping["type"] != "lifespan.shutdown.complete":
                raise RuntimeError(f"app shutdown failed: {stopping}")
            await asyncio.wait_for(lifespan, timeout=5)


@pytest.fixture
def ah():
    yield AsyncHarness()


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


SUPER_KEY = "test-super-key"


# ---------- helpers ----------

class Tenant:
    def __init__(self, id: int, admin_key: str, name: str):
        self.id = id
        self.admin_key = admin_key
        self.name = name


class Device:
    def __init__(self, id: int, token: str, name: str, tenant_id: int):
        self.id = id
        self.token = token
        self.name = name
        self.tenant_id = tenant_id


class AP:
    def __init__(self, id: int, pool_id: int, tenant_id: int, capacity: int):
        self.id = id
        self.pool_id = pool_id
        self.tenant_id = tenant_id
        self.capacity = capacity


def make_tenant(client, name: str | None = None) -> Tenant:
    name = name or f"t-{secrets.token_hex(4)}"
    resp = client.post("/api/v1/tenants", headers={"X-Admin-Key": SUPER_KEY}, json={"name": name})
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return Tenant(data["id"], data["admin_key"], data["name"])


def make_pool(client, tenant: Tenant, cidr: str = "10.10.0.0/24",
              reserved_ips=None, name: str | None = None):
    resp = client.post(
        f"/api/v1/tenants/{tenant.id}/pools",
        headers={"X-Admin-Key": tenant.admin_key},
        json={"name": name or f"p-{secrets.token_hex(4)}", "cidr": cidr,
              "reserved_ips": reserved_ips or []},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_ap(client, tenant: Tenant, pool_id: int, capacity: int = 50,
            name: str | None = None) -> AP:
    resp = client.post(
        f"/api/v1/tenants/{tenant.id}/access-points",
        headers={"X-Admin-Key": tenant.admin_key},
        json={"name": name or f"ap-{secrets.token_hex(4)}", "pool_id": pool_id,
              "capacity": capacity},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return AP(data["id"], data["pool_id"], tenant.id, data["capacity"])


def make_device(client, tenant: Tenant, name: str | None = None) -> Device:
    resp = client.post(
        f"/api/v1/tenants/{tenant.id}/devices",
        headers={"X-Admin-Key": tenant.admin_key},
        json={"name": name or f"d-{secrets.token_hex(4)}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return Device(data["id"], data["device_token"], data["name"], tenant.id)


def connect(client, device: Device, ap_id: int, key: str | None = None, **kwargs):
    body = {"access_point_id": ap_id}
    if key is not None:
        body["idempotency_key"] = key
    return client.post("/api/v1/sessions", headers={"X-Device-Token": device.token}, json=body, **kwargs)


def connect_ok(client, device: Device, ap_id: int, key: str | None = None):
    resp = connect(client, device, ap_id, key)
    assert resp.status_code == 201, resp.text
    return resp.json()


def heartbeat(client, device: Device, session_token: str, **kwargs):
    return client.post(
        "/api/v1/sessions/heartbeat",
        headers={"X-Device-Token": device.token, "X-Session-Token": session_token},
        **kwargs,
    )


def disconnect(client, device: Device, session_token: str, **kwargs):
    return client.post(
        "/api/v1/sessions/disconnect",
        headers={"X-Device-Token": device.token, "X-Session-Token": session_token},
        **kwargs,
    )


def revoke(client, tenant: Tenant, device_id: int):
    return client.post(
        f"/api/v1/tenants/{tenant.id}/devices/{device_id}/revoke",
        headers={"X-Admin-Key": tenant.admin_key},
        json={},
    )


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError("condition not met before timeout")
        time.sleep(interval)
