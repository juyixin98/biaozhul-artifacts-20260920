from conftest import SUPER_KEY, connect, make_ap, make_device, make_pool, make_tenant


def test_auth_required(client):
    tenant = make_tenant(client)
    # admin endpoints need an admin key
    assert client.get(f"/api/v1/tenants/{tenant.id}/devices").status_code == 401
    assert client.get("/api/v1/tenants", headers={"X-Admin-Key": "nope"}).status_code == 401
    # device endpoints need a device token
    resp = client.post("/api/v1/sessions", json={"access_point_id": 1})
    assert resp.status_code == 401


def test_device_token_of_tenant_a_has_no_admin_rights(client):
    a = make_tenant(client)
    dev = make_device(client, a)
    # device token on tenant admin endpoint -> 403 (authenticated as device, not admin)
    resp = client.get(
        f"/api/v1/tenants/{a.id}/devices",
        headers={"X-Device-Token": dev.token},
    )
    assert resp.status_code == 403
    # device token on super-admin endpoint -> 403 as well
    resp = client.get("/api/v1/tenants", headers={"X-Device-Token": dev.token})
    assert resp.status_code == 403
    # a tenant admin key is not the super admin key -> 401
    assert client.get("/api/v1/tenants", headers={"X-Admin-Key": a.admin_key}).status_code == 401


def test_tenant_scoping_of_admin_queries(client):
    a = make_tenant(client)
    b = make_tenant(client)
    pool_a = make_pool(client, a)
    pool_b = make_pool(client, b)
    ap_a = make_ap(client, a, pool_a["id"])
    ap_b = make_ap(client, b, pool_b["id"])
    dev_a = make_device(client, a)
    dev_b = make_device(client, b)

    # A's admin key cannot read B's resources: existence hidden as 404
    assert client.get(
        f"/api/v1/tenants/{b.id}/devices", headers={"X-Admin-Key": a.admin_key}
    ).status_code == 401
    assert client.post(
        f"/api/v1/tenants/{b.id}/devices",
        headers={"X-Admin-Key": a.admin_key}, json={"name": "x"},
    ).status_code == 401

    # super admin can see both
    for tid in (a.id, b.id):
        assert client.get(
            f"/api/v1/tenants/{tid}/devices", headers={"X-Admin-Key": SUPER_KEY}
        ).status_code == 200

    # devices listing is properly scoped
    devices_a = client.get(
        f"/api/v1/tenants/{a.id}/devices", headers={"X-Admin-Key": a.admin_key}
    ).json()
    assert {d["id"] for d in devices_a} == {dev_a.id}

    # revoke across tenants is impossible (wrong tenant key -> 401)
    assert client.post(
        f"/api/v1/tenants/{b.id}/devices/{dev_b.id}/revoke",
        headers={"X-Admin-Key": a.admin_key}, json={},
    ).status_code == 401
    # wrong device id under A's own tenant -> 404
    assert client.post(
        f"/api/v1/tenants/{a.id}/devices/{dev_b.id}/revoke",
        headers={"X-Admin-Key": a.admin_key}, json={},
    ).status_code == 404


def test_device_cannot_connect_to_other_tenants_access_point(client):
    a = make_tenant(client)
    b = make_tenant(client)
    ap_b = make_ap(client, b, make_pool(client, b)["id"])
    dev_a = make_device(client, a)

    resp = connect(client, dev_a, ap_b.id)
    assert resp.status_code == 404

    # and no lease was created anywhere for A
    rows = client.get(
        f"/api/v1/tenants/{a.id}/leases", headers={"X-Admin-Key": a.admin_key}
    ).json()
    assert rows == []


def test_device_token_from_b_rejected_for_a_sessions(client):
    a = make_tenant(client)
    b = make_tenant(client)
    ap_a = make_ap(client, a, make_pool(client, a)["id"])
    dev_b = make_device(client, b)

    # B device tries A's AP id (which does not exist for B) -> hidden 404
    assert connect(client, dev_b, ap_a.id).status_code == 404
