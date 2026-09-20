"""Address-pool allocation: exclusions, exhaustion, reuse."""
from __future__ import annotations

from .helpers import (
    admin_headers,
    connect,
    create_device,
    device_headers,
    make_tenant_env,
)


def _ips_of(client, headers, tid):
    leases = client.get(f"/admin/tenants/{tid}/leases?state=active", headers=headers).json()
    return {l["ip"] for l in leases}


def test_allocation_excludes_network_broadcast_and_reserved(client):
    h = admin_headers(client)
    # /29 -> .0 network, .7 broadcast, .1-.6 usable; reserve .2 as well.
    tid, ap_id = make_tenant_env(client, h, cidr="192.0.2.0/29", reserved=["192.0.2.2"])

    tokens = [create_device(client, h, tid, f"d{i}")[1] for i in range(5)]
    ips = set()
    for i, tok in enumerate(tokens):
        resp = connect(client, tok, ap_id, key=f"k{i}")
        assert resp.status_code == 200, resp.text
        ips.add(resp.json()["ip"])

    assert ips == {"192.0.2.1", "192.0.2.3", "192.0.2.4", "192.0.2.5", "192.0.2.6"}
    assert "192.0.2.0" not in ips and "192.0.2.7" not in ips and "192.0.2.2" not in ips

    # Pool is now exhausted: the sixth device is rejected and nothing leaks.
    _, tok6 = create_device(client, h, tid, "d6")
    resp = connect(client, tok6, ap_id, key="k6")
    assert resp.status_code == 409
    assert "pool exhausted" in resp.json()["detail"]

    pool = client.get(f"/admin/tenants/{tid}/pools", headers=h).json()[0]
    assert pool["usable_addresses"] == 5
    assert pool["allocated_addresses"] == 5


def test_released_ip_is_reallocated(client):
    h = admin_headers(client)
    tid, ap_id = make_tenant_env(client, h, cidr="10.8.0.0/30")  # 2 usable
    _, tok1 = create_device(client, h, tid, "d1")
    _, tok2 = create_device(client, h, tid, "d2")

    r1 = connect(client, tok1, ap_id, key="a").json()
    r2 = connect(client, tok2, ap_id, key="b").json()
    assert r1["ip"] != r2["ip"]

    # Disconnect d1; a new device must be able to take its address.
    client.post(
        "/device/disconnect",
        json={"lease_id": r1["lease_id"], "generation": r1["generation"]},
        headers=device_headers(tok1),
    )
    _, tok3 = create_device(client, h, tid, "d3")
    r3 = connect(client, tok3, ap_id, key="c")
    assert r3.status_code == 200
    assert r3.json()["ip"] == r1["ip"]  # lowest free address is reused
