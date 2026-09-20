"""Concurrency: capacity races, IP uniqueness, idempotent duplicate connects."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from starlette.testclient import TestClient

from .helpers import admin_headers, create_device, device_headers, make_tenant_env


def _connect(app, token, ap_id, key):
    c = TestClient(app)
    resp = c.post(
        "/device/connect",
        json={"access_point_id": ap_id, "idempotency_key": key},
        headers=device_headers(token),
    )
    return resp.status_code, resp.json()


def _active_leases(client, headers, tid):
    return client.get(f"/admin/tenants/{tid}/leases?state=active", headers=headers).json()


def test_capacity_race_never_exceeds_limit(client, app):
    h = admin_headers(client)
    tid, ap_id = make_tenant_env(client, h, cidr="10.10.0.0/24", capacity=1)
    tokens = [create_device(client, h, tid, f"d{i}")[1] for i in range(8)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda t: _connect(app, t, ap_id, f"k-{t[:8]}"), tokens))

    ok = [r for r in results if r[0] == 200]
    rejected = [r for r in results if r[0] == 409]
    assert len(ok) == 1, results
    assert len(rejected) == 7
    assert all("capacity" in r[1]["detail"] for r in rejected)
    assert len(_active_leases(client, h, tid)) == 1


def test_pool_race_allocates_unique_ips_and_no_more(client, app):
    h = admin_headers(client)
    # /29 -> exactly 6 usable addresses; capacity is not the constraint here.
    tid, ap_id = make_tenant_env(client, h, cidr="10.11.0.0/29", capacity=100)
    tokens = [create_device(client, h, tid, f"d{i}")[1] for i in range(10)]

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda t: _connect(app, t, ap_id, f"k-{t[:8]}"), tokens))

    ok = [r for r in results if r[0] == 200]
    exhausted = [r for r in results if r[0] == 409]
    assert len(ok) == 6, results
    assert len(exhausted) == 4
    assert all("pool exhausted" in r[1]["detail"] for r in exhausted)

    ips = [r[1]["ip"] for r in ok]
    assert len(set(ips)) == 6  # no IP was handed out twice
    assert len(_active_leases(client, h, tid)) == 6


def test_duplicate_connect_race_is_idempotent(client, app):
    h = admin_headers(client)
    tid, ap_id = make_tenant_env(client, h, capacity=10)
    _, token = create_device(client, h, tid)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: _connect(app, token, ap_id, "same-key"), range(6)))

    assert all(code == 200 for code, _ in results)
    lease_ids = {body["lease_id"] for _, body in results}
    assert len(lease_ids) == 1  # everybody got the same lease
    created = [body for _, body in results if body["reused"] is False]
    assert len(created) == 1  # exactly one caller actually created it

    active = _active_leases(client, h, tid)
    assert len(active) == 1
    assert active[0]["id"] == lease_ids.pop()
