import asyncio

from conftest import connect, connect_ok, disconnect, make_ap, make_device, make_pool, make_tenant


def test_connect_returns_session(client):
    tenant = make_tenant(client)
    pool = make_pool(client, tenant)
    ap = make_ap(client, tenant, pool["id"], capacity=10)
    dev = make_device(client, tenant)

    sess = connect_ok(client, dev, ap.id)
    lease = sess["lease"]
    assert lease["ip_address"]
    assert lease["status"] == "active"
    assert lease["generation"] == 1
    assert sess["session_token"]
    assert sess["heartbeat_interval_seconds"] == 60


def test_second_connect_without_key_returns_existing_session(client):
    tenant = make_tenant(client)
    ap = make_ap(client, tenant, make_pool(client, tenant)["id"], capacity=10)
    dev = make_device(client, tenant)

    first = connect_ok(client, dev, ap.id)
    second = connect_ok(client, dev, ap.id)
    assert first["lease"]["id"] == second["lease"]["id"]
    assert first["lease"]["ip_address"] == second["lease"]["ip_address"]


def test_repeated_connect_is_idempotent_same_key(client):
    tenant = make_tenant(client)
    ap = make_ap(client, tenant, make_pool(client, tenant)["id"], capacity=10)
    dev = make_device(client, tenant)

    results = [connect(client, dev, ap.id, key="fixed-key") for _ in range(5)]
    ids = {r.json()["lease"]["id"] for r in results}
    assert ids == {results[0].json()["lease"]["id"]}


def test_idempotency_key_unique_per_device(client):
    tenant = make_tenant(client)
    ap = make_ap(client, tenant, make_pool(client, tenant)["id"], capacity=10)
    dev = make_device(client, tenant)

    first = connect_ok(client, dev, ap.id, key="once")
    assert disconnect(client, dev, _token_for(client, first)).status_code == 200
    # key cannot be reused even after close
    resp = connect(client, dev, ap.id, key="once")
    assert resp.status_code == 409


def test_idempotency_key_not_resurrected_after_close(client):
    tenant = make_tenant(client)
    ap = make_ap(client, tenant, make_pool(client, tenant)["id"], capacity=10)
    dev = make_device(client, tenant)

    sess = connect_ok(client, dev, ap.id, key="resurrect")
    assert disconnect(client, dev, sess["session_token"]).status_code == 200
    # connect without a key creates a fresh lease
    new = connect_ok(client, dev, ap.id)
    assert new["lease"]["id"] != sess["lease"]["id"]
    assert new["lease"]["generation"] == 2
    # ... but the old key is still poisoned
    assert connect(client, dev, ap.id, key="resurrect").status_code == 409


async def _apost(ac, token, ap_id):
    return await ac.post(
        "/api/v1/sessions",
        headers={"X-Device-Token": token},
        json={"access_point_id": ap_id},
    )


def test_concurrent_connects_get_unique_ips(client, ah):
    tenant = make_tenant(client)
    ap = make_ap(client, tenant, make_pool(client, tenant)["id"], capacity=100)
    devices = [make_device(client, tenant) for _ in range(40)]

    async def body(ac):
        responses = await asyncio.gather(*[_apost(ac, d.token, ap.id) for d in devices])
        return responses

    responses = ah.run(body)
    assert all(r.status_code == 201 for r in responses)
    ips = [r.json()["lease"]["ip_address"] for r in responses]
    assert len(set(ips)) == 40


def test_one_active_lease_per_device_under_concurrency(client, ah):
    tenant = make_tenant(client)
    ap = make_ap(client, tenant, make_pool(client, tenant)["id"], capacity=100)
    dev = make_device(client, tenant)

    async def body(ac):
        return await asyncio.gather(*[_apost(ac, dev.token, ap.id) for _ in range(25)])

    responses = ah.run(body)
    assert all(r.status_code == 201 for r in responses)
    lease_ids = {r.json()["lease"]["id"] for r in responses}
    assert len(lease_ids) == 1

    rows = client.get(
        f"/api/v1/tenants/{tenant.id}/leases",
        headers={"X-Admin-Key": tenant.admin_key},
    ).json()
    active = [r for r in rows if r["status"] == "active"]
    assert len(active) == 1
    assert active[0]["id"] in lease_ids


def test_capacity_race_never_exceeds_capacity(client, ah):
    tenant = make_tenant(client)
    # big pool so capacity, not addresses, is the contested limit
    ap = make_ap(client, tenant, make_pool(client, tenant, cidr="10.77.0.0/16")["id"], capacity=20)
    devices = [make_device(client, tenant) for _ in range(60)]

    async def body(ac):
        return await asyncio.gather(*[_apost(ac, d.token, ap.id) for d in devices])

    responses = ah.run(body)
    successes = [r for r in responses if r.status_code == 201]
    rejected = [r for r in responses if r.status_code == 409]
    assert len(successes) == 20
    assert len(rejected) == 40

    detail = client.get(
        f"/api/v1/tenants/{tenant.id}/access-points",
        headers={"X-Admin-Key": tenant.admin_key},
    ).json()
    assert detail[0]["active_leases"] == 20


def _token_for(client, sess):
    return sess["session_token"]

