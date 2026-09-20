#!/usr/bin/env python3
"""End-to-end demo against a running CloudGate server.

Usage:  python3 scripts/demo.py [base_url]     (default http://127.0.0.1:8000)

Walks through: tenant/pool/AP/device provisioning, connect, idempotent retry,
heartbeat, pool exhaustion, revocation, and termination records.
"""
from __future__ import annotations

import sys
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin123"


def step(msg: str) -> None:
    print(f"\n=== {msg} ===")


def show(resp: httpx.Response) -> None:
    print(f"{resp.request.method} {resp.request.url.path} -> {resp.status_code}")
    try:
        body = resp.json()
    except Exception:
        return
    if resp.status_code >= 400 or isinstance(body, (dict, list)):
        print(f"    {body}")


def main() -> None:
    c = httpx.Client(base_url=BASE, timeout=10)

    step("admin login")
    r = c.post("/admin/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    show(r)
    r.raise_for_status()
    admin = {"Authorization": f"Bearer {r.json()['access_token']}"}

    suffix = uuid.uuid4().hex[:6]
    step("provision tenant, /29 pool (6 usable IPs, .2 reserved), access point, devices")
    r = c.post("/admin/tenants", json={"name": f"demo-{suffix}"}, headers=admin)
    show(r)
    tid = r.json()["id"]

    r = c.post(
        f"/admin/tenants/{tid}/pools",
        json={"name": "pool-1", "cidr": "203.0.113.0/29", "reserved": ["203.0.113.2"]},
        headers=admin,
    )
    show(r)
    pool_id = r.json()["id"]

    r = c.post(
        f"/admin/tenants/{tid}/access-points",
        json={"name": "ap-1", "pool_id": pool_id, "capacity": 4},
        headers=admin,
    )
    show(r)
    ap_id = r.json()["id"]

    tokens = []
    for i in range(3):
        r = c.post(f"/admin/tenants/{tid}/devices", json={"name": f"sensor-{i}"}, headers=admin)
        tokens.append(r.json()["token"])
        print(f"    device sensor-{i} token: {r.json()['token'][:16]}...")

    def dev(i: int) -> dict:
        return {"Authorization": f"Bearer {tokens[i]}"}

    step("device 0 connects (idempotency key demo-key-0)")
    r = c.post("/device/connect", json={"access_point_id": ap_id, "idempotency_key": "demo-key-0"}, headers=dev(0))
    show(r)
    lease0 = r.json()

    step("same request retried -> same lease, reused=true")
    r = c.post("/device/connect", json={"access_point_id": ap_id, "idempotency_key": "demo-key-0"}, headers=dev(0))
    show(r)

    step("device 0 connects again with a NEW key while active -> 409")
    r = c.post("/device/connect", json={"access_point_id": ap_id, "idempotency_key": "other-key"}, headers=dev(0))
    show(r)

    step("devices 1 and 2 connect; note the reserved .2 is skipped")
    leases = [lease0]
    for i in (1, 2):
        r = c.post("/device/connect", json={"access_point_id": ap_id, "idempotency_key": f"demo-key-{i}"}, headers=dev(i))
        show(r)
        leases.append(r.json())

    step("device 0 heartbeats (wrong generation -> 409, then correct -> 200)")
    r = c.post("/device/heartbeat", json={"lease_id": lease0["lease_id"], "generation": 999}, headers=dev(0))
    show(r)
    r = c.post(
        "/device/heartbeat",
        json={"lease_id": lease0["lease_id"], "generation": lease0["generation"]},
        headers=dev(0),
    )
    show(r)

    step("admin revokes device 1 -> its session is terminated immediately")
    devices = c.get(f"/admin/tenants/{tid}/devices", headers=admin).json()
    dev1_id = [d for d in devices if d["name"] == "sensor-1"][0]["id"]
    r = c.post(f"/admin/tenants/{tid}/devices/{dev1_id}/revoke", headers=admin)
    show(r)

    step("revoked device's token is dead (heartbeat -> 401)")
    r = c.post(
        "/device/heartbeat",
        json={"lease_id": leases[1]["lease_id"], "generation": leases[1]["generation"]},
        headers=dev(1),
    )
    show(r)

    step("device 2 disconnects cleanly")
    r = c.post(
        "/device/disconnect",
        json={"lease_id": leases[2]["lease_id"], "generation": leases[2]["generation"]},
        headers=dev(2),
    )
    show(r)

    step("termination records (reason + time, one per lease)")
    r = c.get(f"/admin/tenants/{tid}/terminations", headers=admin)
    show(r)

    step("final lease table")
    r = c.get(f"/admin/tenants/{tid}/leases", headers=admin)
    for lease in r.json():
        print(
            f"    {lease['ip']:>15}  gen={lease['generation']}  {lease['state']}"
            + (f"  ({lease['release_reason']})" if lease["release_reason"] else "")
        )

    print("\nDemo complete.")


if __name__ == "__main__":
    main()
