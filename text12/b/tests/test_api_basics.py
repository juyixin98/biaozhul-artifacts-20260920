"""Smoke tests: health, auth flows, pool/AP validation, audit trail."""

import uuid

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import LeaseTermination, Session as SessionModel


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_admin_login_wrong_password(client):
    from app.config import settings

    r = client.post(
        "/api/admin/login",
        json={"username": settings.platform_admin_username, "password": "wrong"},
    )
    assert r.status_code == 401


def test_capacity_cannot_exceed_usable_pool(client, make_pool):
    pool = make_pool(cidr="10.4.0.0/29", reserved=["10.4.0.1"])  # 5 usable
    tenant = pool["tenant"]
    r = client.post(
        f"{tenant['base']}/access-points",
        headers={"Authorization": f"Bearer {tenant['token']}"},
        json={"name": "too-big", "pool_id": pool["id"], "capacity": 6},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "capacity_exceeds_pool"


def test_pool_overlap_rejected(client, make_pool):
    pool = make_pool(cidr="10.8.0.0/24")
    tenant = pool["tenant"]
    r = client.post(
        f"{tenant['base']}/pools",
        headers={"Authorization": f"Bearer {tenant['token']}"},
        json={"name": "p2", "cidr": "10.8.0.128/25", "reserved_addresses": []},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "pool_overlaps"


def test_close_audit_trail(client, make_ap, make_device, api_connect):
    ap = make_ap(cidr="10.90.0.0/24", capacity=5)
    tenant = ap["tenant"]
    device = make_device(tenant, name="audited")
    lease = api_connect(device, ap).json()

    r = client.post(
        "/api/device/close",
        headers={"X-Device-Token": device["token"], "X-Lease-Token": lease["lease_token"]},
        json={"session_id": lease["session"]["id"], "generation": lease["session"]["generation"]},
    )
    assert r.status_code == 200 and r.json()["status"] == "closed"

    r = client.get(
        f"{tenant['base']}/sessions/{lease['session']['id']}/termination",
        headers={"Authorization": f"Bearer {tenant['token']}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["reason"] == "client_close"
    assert body["terminated_at"]

    # Active session -> 404 on termination lookup
    device2 = make_device(tenant, name="active-one")
    lease2 = api_connect(device2, ap).json()
    r = client.get(
        f"{tenant['base']}/sessions/{lease2['session']['id']}/termination",
        headers={"Authorization": f"Bearer {tenant['token']}"},
    )
    assert r.status_code == 404


def test_device_calls_without_token_unauthorized(client, make_ap):
    ap = make_ap(cidr="10.91.0.0/24", capacity=2)
    r = client.post(
        "/api/device/connect",
        json={"access_point_id": ap["id"], "idempotency_key": "k"},
    )
    assert r.status_code == 401
