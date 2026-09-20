"""HTTP-level helpers shared by the test modules."""
from __future__ import annotations

import uuid

from starlette.testclient import TestClient

from .conftest import ADMIN_PASSWORD, ADMIN_USERNAME


def admin_headers(client: TestClient, username: str = ADMIN_USERNAME, password: str = ADMIN_PASSWORD) -> dict:
    resp = client.post("/admin/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def create_tenant(client: TestClient, headers: dict, name: str | None = None) -> str:
    name = name or f"tenant-{uuid.uuid4().hex[:8]}"
    resp = client.post("/admin/tenants", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def create_pool(client: TestClient, headers: dict, tenant_id: str, cidr: str,
                reserved: list[str] | None = None, name: str | None = None) -> str:
    name = name or f"pool-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        f"/admin/tenants/{tenant_id}/pools",
        json={"name": name, "cidr": cidr, "reserved": reserved or []},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def create_ap(client: TestClient, headers: dict, tenant_id: str, pool_id: str,
              capacity: int = 10, name: str | None = None) -> str:
    name = name or f"ap-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        f"/admin/tenants/{tenant_id}/access-points",
        json={"name": name, "pool_id": pool_id, "capacity": capacity},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def create_device(client: TestClient, headers: dict, tenant_id: str,
                  name: str | None = None) -> tuple[str, str]:
    name = name or f"dev-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        f"/admin/tenants/{tenant_id}/devices", json={"name": name}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["id"], body["token"]


def device_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def connect(client: TestClient, token: str, ap_id: str, key: str | None = None):
    key = key or uuid.uuid4().hex
    return client.post(
        "/device/connect",
        json={"access_point_id": ap_id, "idempotency_key": key},
        headers=device_headers(token),
    )


def make_tenant_env(client: TestClient, headers: dict, cidr: str = "10.0.0.0/29",
                    capacity: int = 10, reserved: list[str] | None = None):
    """Create tenant + pool + AP; return (tenant_id, ap_id)."""
    tid = create_tenant(client, headers)
    pool_id = create_pool(client, headers, tid, cidr, reserved=reserved)
    ap_id = create_ap(client, headers, tid, pool_id, capacity=capacity)
    return tid, ap_id
