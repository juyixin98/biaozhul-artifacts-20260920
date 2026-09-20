"""Address allocation: network/broadcast/reserved exclusions and exhaustion."""

from app.networking import expand_pool, parse_cidr, usable_count


def test_network_and_broadcast_are_non_allocatable():
    rows = expand_pool("192.168.5.0/29")
    kinds = {ip: kind for ip, kind, _ in rows}
    assert kinds["192.168.5.0"] == "network"
    assert kinds["192.168.5.7"] == "broadcast"
    usable = [ip for ip, kind, reserved in rows if kind == "usable" and not reserved]
    assert usable == [f"192.168.5.{i}" for i in range(1, 7)]


def test_reserved_addresses_excluded():
    rows = expand_pool("10.0.0.0/30", reserved=["10.0.0.1"])
    # /30: .0 network, .3 broadcast, .1 reserved gateway, .2 the only usable host
    assert usable_count("10.0.0.0/30", reserved=["10.0.0.1"]) == 1
    assert all(not (kind == "usable" and reserved and ip != "10.0.0.1")
               for ip, kind, reserved in rows)


def test_reserved_outside_cidr_rejected():
    import pytest

    with pytest.raises(ValueError):
        expand_pool("10.0.0.0/30", reserved=["10.0.0.9"])


def test_reserving_network_or_broadcast_rejected():
    import pytest

    with pytest.raises(ValueError):
        expand_pool("10.0.0.0/30", reserved=["10.0.0.0"])
    with pytest.raises(ValueError):
        expand_pool("10.0.0.0/30", reserved=["10.0.0.3"])


def test_invalid_cidr_rejected():
    import pytest

    for bad in ("not-a-cidr", "10.0.0.0/33", "::1/127", "10.0.0.1/24"):
        with pytest.raises(ValueError):
            parse_cidr(bad)


def test_pool_end_to_end_exhaustion(client, make_ap, make_device, api_connect):
    # /29: 6 hosts, reserve one -> 5 allocatable; AP capacity 5.
    ap = make_ap(cidr="172.16.9.0/29", reserved=["172.16.9.1"], capacity=5)
    tenant = ap["tenant"]

    leases = []
    for i in range(5):
        dev = make_device(tenant, name=f"d{i}")
        resp = api_connect(dev, ap)
        assert resp.status_code == 201, resp.text
        leases.append(resp.json())

    ips = sorted(l["session"]["ip"] for l in leases)
    assert len(set(ips)) == 5
    assert "172.16.9.0" not in ips and "172.16.9.7" not in ips
    assert "172.16.9.1" not in ips  # reserved

    # Pool exhausted: a 6th device cannot get an IP even though capacity allows 5.
    extra = make_device(tenant, name="extra")
    # AP is full first -> capacity error.
    resp = api_connect(extra, ap)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "capacity_exceeded"


def test_pool_exhausted_before_capacity(client, make_ap, make_device, api_connect):
    # /29 with one reserved -> 5 allocatable addresses, AP capacity 5.
    ap = make_ap(cidr="172.16.10.0/29", reserved=["172.16.10.1"], capacity=5)
    tenant = ap["tenant"]
    leases = []
    devices = []
    for i in range(5):
        dev = make_device(tenant, name=f"d{i}")
        r = api_connect(dev, ap)
        assert r.status_code == 201, r.text
        leases.append(r.json())
        devices.append(dev)

    # Close two leases: capacity frees up, but their addresses immediately
    # become available... instead prove exhaustion differently: use a second
    # AP on the SAME pool. Its capacity is satisfied, yet no IP is left.
    r = client.post(
        f"{tenant['base']}/access-points",
        headers={"Authorization": f"Bearer {tenant['token']}"},
        json={"name": "ap2", "pool_id": ap["pool_id"], "capacity": 5},
    )
    assert r.status_code == 201, r.text
    ap2 = r.json()

    extra = make_device(tenant, name="extra")
    resp = api_connect(extra, ap2)
    # ap2 is empty (capacity nowhere near exceeded), but the shared pool is dry
    assert resp.status_code == 507
    assert resp.json()["error"]["code"] == "address_pool_exhausted"


def test_freed_address_is_reallocatable(client, make_ap, make_device, api_connect):
    ap = make_ap(cidr="172.30.0.0/30", reserved=[], capacity=2)  # .1,.2 usable
    tenant = ap["tenant"]
    d1 = make_device(tenant, name="d1")
    d2 = make_device(tenant, name="d2")
    l1 = api_connect(d1, ap).json()
    api_connect(d2, ap)

    # close d1 -> its address returns to the pool
    resp = client.post(
        "/api/device/close",
        headers={"X-Device-Token": d1["token"], "X-Lease-Token": l1["lease_token"]},
        json={"session_id": l1["session"]["id"], "generation": l1["session"]["generation"]},
    )
    assert resp.status_code == 200

    d3 = make_device(tenant, name="d3")
    l3 = api_connect(d3, ap)
    assert l3.status_code == 201
    assert l3.json()["session"]["ip"] == l1["session"]["ip"]
