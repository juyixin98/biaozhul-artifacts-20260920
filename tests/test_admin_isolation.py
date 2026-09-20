"""Admin API and tenant-isolation tests."""
from __future__ import annotations

from .conftest import ADMIN_PASSWORD
from .helpers import (
    admin_headers,
    create_ap,
    create_device,
    create_pool,
    create_tenant,
    device_headers,
    make_tenant_env,
)


def test_login_rejects_bad_credentials(client):
    resp = client.post("/admin/login", json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401


def test_admin_crud_flow(client):
    h = admin_headers(client)
    tid = create_tenant(client, h, "acme")
    pool_id = create_pool(client, h, tid, "10.1.0.0/29")
    ap_id = create_ap(client, h, tid, pool_id, capacity=5)
    dev_id, token = create_device(client, h, tid)

    pools = client.get(f"/admin/tenants/{tid}/pools", headers=h).json()
    assert pools[0]["usable_addresses"] == 6  # /29 minus network+broadcast
    assert pools[0]["allocated_addresses"] == 0

    aps = client.get(f"/admin/tenants/{tid}/access-points", headers=h).json()
    assert aps[0]["capacity"] == 5 and aps[0]["active_sessions"] == 0

    devices = client.get(f"/admin/tenants/{tid}/devices", headers=h).json()
    assert devices[0]["id"] == dev_id
    assert devices[0]["revoked"] is False
    assert "token" not in devices[0]  # token is only returned at creation


def test_duplicate_names_conflict(client):
    h = admin_headers(client)
    tid = create_tenant(client, h, "dup-check")
    resp = client.post("/admin/tenants", json={"name": "dup-check"}, headers=h)
    assert resp.status_code == 409
    create_pool(client, h, tid, "10.2.0.0/29", name="p1")
    resp = client.post(
        f"/admin/tenants/{tid}/pools", json={"name": "p1", "cidr": "10.3.0.0/29"}, headers=h
    )
    assert resp.status_code == 409


def test_reserved_address_must_be_inside_pool(client):
    h = admin_headers(client)
    tid = create_tenant(client, h)
    resp = client.post(
        f"/admin/tenants/{tid}/pools",
        json={"name": "p", "cidr": "10.4.0.0/29", "reserved": ["10.4.1.5"]},
        headers=h,
    )
    assert resp.status_code == 422


def test_tenant_scoped_admin_isolated_from_other_tenants(client):
    h = admin_headers(client)
    tid_a, _ = make_tenant_env(client, h)
    tid_b, _ = make_tenant_env(client, h)

    # Create a tenant-scoped admin for tenant A and log in as them.
    resp = client.post(
        f"/admin/tenants/{tid_a}/admins",
        json={"username": "admin-a", "password": "pw-aaaa"},
        headers=h,
    )
    assert resp.status_code == 201
    ha = admin_headers(client, "admin-a", "pw-aaaa")

    # Can see own tenant, cannot see or modify tenant B (404, not 403).
    assert client.get("/admin/tenants", headers=ha).json()[0]["id"] == tid_a
    assert client.get(f"/admin/tenants/{tid_b}/devices", headers=ha).status_code == 404
    assert client.get(f"/admin/tenants/{tid_b}/leases", headers=ha).status_code == 404
    resp = client.post(
        f"/admin/tenants/{tid_b}/pools", json={"name": "x", "cidr": "10.5.0.0/29"}, headers=ha
    )
    assert resp.status_code == 404
    # Tenant admin cannot create tenants (global-only).
    assert client.post("/admin/tenants", json={"name": "nope"}, headers=ha).status_code == 403


def test_device_cannot_use_other_tenants_access_point(client):
    h = admin_headers(client)
    tid_a, ap_a = make_tenant_env(client, h)
    tid_b, ap_b = make_tenant_env(client, h)
    _, token_a = create_device(client, h, tid_a)

    resp = client.post(
        "/device/connect",
        json={"access_point_id": ap_b, "idempotency_key": "k1"},
        headers=device_headers(token_a),
    )
    assert resp.status_code == 404  # AP of tenant B is invisible to tenant A's device

    # Sanity: same device can use its own tenant's AP.
    resp = client.post(
        "/device/connect",
        json={"access_point_id": ap_a, "idempotency_key": "k2"},
        headers=device_headers(token_a),
    )
    assert resp.status_code == 200


def test_device_cannot_touch_other_devices_lease(client):
    h = admin_headers(client)
    tid, ap_id = make_tenant_env(client, h)
    _, token1 = create_device(client, h, tid, "d1")
    _, token2 = create_device(client, h, tid, "d2")

    lease = client.post(
        "/device/connect",
        json={"access_point_id": ap_id, "idempotency_key": "k1"},
        headers=device_headers(token1),
    ).json()

    # Device 2 presenting device 1's lease_id gets 404 regardless of generation.
    resp = client.post(
        "/device/heartbeat",
        json={"lease_id": lease["lease_id"], "generation": lease["generation"]},
        headers=device_headers(token2),
    )
    assert resp.status_code == 404
    resp = client.post(
        "/device/disconnect",
        json={"lease_id": lease["lease_id"], "generation": lease["generation"]},
        headers=device_headers(token2),
    )
    assert resp.status_code == 404


def test_unauthenticated_requests_rejected(client):
    assert client.get("/admin/tenants").status_code == 401
    assert client.post("/device/connect", json={}).status_code == 401
    assert client.post(
        "/device/connect", json={}, headers={"Authorization": "Bearer bogus"}
    ).status_code == 401
