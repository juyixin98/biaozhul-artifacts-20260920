"""Strict tenant isolation for admin queries and device operations."""

import uuid


def test_tenant_admin_cannot_see_other_tenant_resources(client, make_tenant, make_pool, make_ap, make_device, api_connect):
    t1 = make_tenant("acme")
    t2 = make_tenant("globex")
    p1 = make_pool(t1, cidr="10.1.0.0/24")
    p2 = make_pool(t2, cidr="10.2.0.0/24")
    ap1 = make_ap(p1, capacity=5)
    ap2 = make_ap(p2, capacity=5)
    d1 = make_device(t1, name="d1")
    d2 = make_device(t2, name="d2")
    l1 = api_connect(d1, ap1).json()
    l2 = api_connect(d2, ap2).json()

    auth1 = {"Authorization": f"Bearer {t1['token']}"}
    auth2 = {"Authorization": f"Bearer {t2['token']}"}

    # Other tenant's pool / ap / device listing is empty
    for path in ("pools", "access-points", "devices", "sessions"):
        r = client.get(f"{t2['base']}/{path}", headers=auth1)
        assert r.status_code == 404, path

    # Direct id access to another tenant's resources -> 404
    r = client.get(f"{t1['base']}/sessions", headers=auth2)
    assert r.status_code == 404

    r = client.get(f"{t2['base']}/sessions/{l1['session']['id']}/termination", headers=auth2)
    assert r.status_code == 404

    # Device from t1 cannot connect to t2's access point
    r = client.post(
        "/api/device/connect",
        headers={"X-Device-Token": d1["token"]},
        json={"access_point_id": ap2["id"], "idempotency_key": "x"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "cross_tenant"

    # Device from t2 cannot heartbeat/close t1's session with a guessed token
    r = client.post(
        "/api/device/heartbeat",
        headers={"X-Device-Token": d2["token"], "X-Lease-Token": "lt_guess"},
        json={"session_id": l1["session"]["id"], "generation": 1},
    )
    assert r.status_code == 404  # session does not exist *for this device*


def test_tenant_admin_cannot_use_platform_endpoints(client, make_tenant):
    t = make_tenant("smallco")
    r = client.post(
        "/api/platform/tenants",
        headers={"Authorization": f"Bearer {t['token']}"},
        json={"name": "evil"},
    )
    assert r.status_code == 403


def test_platform_admin_can_access_any_tenant(client, platform_token, make_tenant):
    t = make_tenant("visible")
    r = client.get(f"{t['base']}/sessions", headers={"Authorization": f"Bearer {platform_token}"})
    assert r.status_code == 200
    assert r.json() == []


def test_device_token_from_other_tenant_rejected_for_revoke_adjacent(client, make_ap, make_device, api_connect):
    ap = make_ap(cidr="10.3.0.0/24", capacity=5)
    tenant = ap["tenant"]
    device = make_device(tenant, name="d")
    lease = api_connect(device, ap).json()

    # No authorization header -> 401
    r = client.get(f"{tenant['base']}/sessions")
    assert r.status_code == 401

    # Garbage bearer -> 401
    r = client.get(f"{tenant['base']}/sessions", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401

    # Device credential is not an admin credential
    r = client.get(f"{tenant['base']}/sessions",
                   headers={"Authorization": f"Bearer {device['token']}"})
    assert r.status_code == 401
