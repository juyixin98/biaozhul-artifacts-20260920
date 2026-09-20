import ipaddress

from conftest import connect_ok, make_ap, make_device, make_pool, make_tenant
from app.allocation import host_addresses, pick_free_address, pool_stats


def test_allocate_skips_network_and_broadcast(client):
    tenant = make_tenant(client)
    # /30 -> hosts .1 and .2; .0 is network, .3 broadcast
    pool = make_pool(client, tenant, cidr="10.0.5.0/30")
    ap = make_ap(client, tenant, pool["id"], capacity=10)

    seen = set()
    for _ in range(2):
        dev = make_device(client, tenant)
        sess = connect_ok(client, dev, ap.id)
        ip = sess["lease"]["ip_address"]
        assert ip in ("10.0.5.1", "10.0.5.2")
        seen.add(ip)
    assert seen == {"10.0.5.1", "10.0.5.2"}

    # exhausted now
    third = make_device(client, tenant)
    resp = client.post(
        "/api/v1/sessions", headers={"X-Device-Token": third.token}, json={"access_point_id": ap.id}
    )
    assert resp.status_code == 409


def test_allocate_skips_reserved(client):
    tenant = make_tenant(client)
    pool = make_pool(client, tenant, cidr="10.0.6.0/29", reserved_ips=["10.0.6.2", "10.0.6.3"])
    ap = make_ap(client, tenant, pool["id"], capacity=10)

    allocated = set()
    # .1,.4,.5,.6 usable = 4
    for _ in range(4):
        dev = make_device(client, tenant)
        sess = connect_ok(client, dev, ap.id)
        allocated.add(sess["lease"]["ip_address"])
    assert allocated == {"10.0.6.1", "10.0.6.4", "10.0.6.5", "10.0.6.6"}


def test_pool_reports_usable_excluding_reserved(client):
    tenant = make_tenant(client)
    pool = make_pool(client, tenant, cidr="192.168.9.0/28", reserved_ips=["192.168.9.5"])
    assert pool["total_hosts"] == 14
    assert pool["reserved_count"] == 1
    assert pool["usable"] == 13


def test_reserved_outside_cidr_rejected(client):
    tenant = make_tenant(client)
    resp = client.post(
        f"/api/v1/tenants/{tenant.id}/pools",
        headers={"X-Admin-Key": tenant.admin_key},
        json={"name": "bad", "cidr": "10.1.0.0/24", "reserved_ips": ["10.2.0.1"]},
    )
    assert resp.status_code == 422
    assert "outside" in resp.text


def test_never_assigns_network_broadcast_or_reserved(client):
    cidr = "172.20.3.0/24"
    net = ipaddress.ip_network(cidr)
    reserved = ["172.20.3.10", "172.20.3.20"]
    tenant = make_tenant(client)
    pool = make_pool(client, tenant, cidr=cidr, reserved_ips=reserved)
    ap = make_ap(client, tenant, pool["id"], capacity=300)

    forbidden = {str(net.network_address), str(net.broadcast_address), *reserved}
    for _ in range(30):
        dev = make_device(client, tenant)
        ip = connect_ok(client, dev, ap.id)["lease"]["ip_address"]
        assert ip not in forbidden


def test_address_exhaustion_returns_409(client):
    tenant = make_tenant(client)
    pool = make_pool(client, tenant, cidr="10.9.9.0/30")  # 2 usable hosts
    ap = make_ap(client, tenant, pool["id"], capacity=100)  # capacity not the limit

    for _ in range(2):
        connect_ok(client, make_device(client, tenant), ap.id)

    extra = make_device(client, tenant)
    resp = client.post(
        "/api/v1/sessions", headers={"X-Device-Token": extra.token}, json={"access_point_id": ap.id}
    )
    assert resp.status_code == 409
    assert "exhaust" in resp.json()["detail"]


def test_allocation_helper_units():
    assert host_addresses("10.0.0.0/30") == ["10.0.0.1", "10.0.0.2"]
    total, reserved, usable = pool_stats("10.0.0.0/30", ["10.0.0.1"])
    assert (total, reserved, usable) == (2, 1, 1)
    assert pick_free_address("10.0.0.0/30", [], {"10.0.0.1"}) == "10.0.0.2"
    assert pick_free_address("10.0.0.0/30", ["10.0.0.2"], {"10.0.0.1"}) is None
