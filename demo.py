#!/usr/bin/env python3
"""End-to-end demonstration against a running CloudGate API.

Usage:
    python demo.py                      # defaults to http://127.0.0.1:18099
    BASE_URL=http://127.0.0.1:8000 python demo.py

It provisions a tenant, pool (10.0.0.0/30 -> one usable host because one
address is reserved), an access point and two devices, then walks through
connect, heartbeat, address exhaustion, generation validation, revoke and
the admin's lease audit view.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:18099")
SUPER_KEY = os.environ.get("CLOUDGATE_SUPER_ADMIN_KEY", "super-admin-key")


def call(method: str, path: str, *, device_token: str | None = None,
         session_token: str | None = None, admin_key: str | None = None,
         body: dict | None = None, expect: int | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE_URL + path, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if admin_key:
        req.add_header("X-Admin-Key", admin_key)
    if device_token:
        req.add_header("X-Device-Token", device_token)
    if session_token:
        req.add_header("X-Session-Token", session_token)
    try:
        with urllib.request.urlopen(req) as resp:
            status_code, payload = resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        status_code, payload = exc.code, json.loads(exc.read() or b"{}")
    if expect is not None and status_code != expect:
        raise SystemExit(f"FAIL: {method} {path} expected {expect}, got {status_code}: {payload}")
    print(f"{method:5} {path:58} -> {status_code}")
    return status_code, payload


def main() -> None:
    suffix = uuid.uuid4().hex[:8]
    print(f"== CloudGate demo against {BASE_URL} (run {suffix}) ==\n")

    _, tenant = call("POST", "/api/v1/tenants", admin_key=SUPER_KEY,
                     body={"name": f"tenant-{suffix}"}, expect=201)
    tenant_id = tenant["id"]
    admin_key = tenant["admin_key"]
    print(f"  tenant id={tenant_id}, tenant admin key (shown once): {admin_key[:10]}...\n")

    _, pool = call("POST", f"/api/v1/tenants/{tenant_id}/pools", admin_key=admin_key,
                   body={"name": f"pool-{suffix}", "cidr": "10.0.0.0/30",
                         "reserved_ips": ["10.0.0.2"]}, expect=201)
    assert pool["usable"] == 1, pool  # .1 usable; .0 network, .3 broadcast, .2 reserved
    pool_id = pool["id"]
    print(f"  pool usable = {pool['usable']} (network/broadcast/reserved excluded)\n")

    _, ap = call("POST", f"/api/v1/tenants/{tenant_id}/access-points", admin_key=admin_key,
                 body={"name": f"ap-{suffix}", "pool_id": pool_id, "capacity": 5}, expect=201)
    ap_id = ap["id"]

    devices = []
    for name in ("alpha", "beta"):
        _, dev = call("POST", f"/api/v1/tenants/{tenant_id}/devices", admin_key=admin_key,
                      body={"name": f"{name}-{suffix}"}, expect=201)
        devices.append((dev["id"], dev["device_token"]))

    # alpha connects -> 10.0.0.1
    _, sess1 = call("POST", "/api/v1/sessions", device_token=devices[0][1],
                    body={"access_point_id": ap_id, "idempotency_key": "k1"}, expect=201)
    print(f"  alpha got {sess1['lease']['ip_address']} (generation {sess1['lease']['generation']})\n")

    # repeated connect is idempotent (same lease)
    _, again = call("POST", "/api/v1/sessions", device_token=devices[0][1],
                    body={"access_point_id": ap_id, "idempotency_key": "k1"}, expect=201)
    assert again["lease"]["id"] == sess1["lease"]["id"]

    # reusing the same idempotency key after the session closes must fail
    call("POST", "/api/v1/sessions/disconnect", device_token=devices[0][1],
         session_token=sess1["session_token"], expect=200)
    _, err = call("POST", "/api/v1/sessions", device_token=devices[0][1],
                  body={"access_point_id": ap_id, "idempotency_key": "k1"}, expect=409)
    print(f"  reused idempotency key rejected: {err['detail']}")

    # heartbeat keeps the lease alive
    _, reconnect = call("POST", "/api/v1/sessions", device_token=devices[0][1],
                        body={"access_point_id": ap_id}, expect=201)
    call("POST", "/api/v1/sessions/heartbeat", device_token=devices[0][1],
         session_token=reconnect["session_token"], expect=200)
    assert reconnect["lease"]["generation"] == 2

    # old-generation token must not drive the new session
    call("POST", "/api/v1/sessions/heartbeat", device_token=devices[0][1],
         session_token=sess1["session_token"], expect=409)
    call("POST", "/api/v1/sessions/disconnect", device_token=devices[0][1],
         session_token=sess1["session_token"], expect=409)
    print("  stale generation heartbeat/disconnect rejected with 409\n")

    # beta cannot get an address: .1 is held, .2 reserved
    _, err = call("POST", "/api/v1/sessions", device_token=devices[1][1],
                  body={"access_point_id": ap_id}, expect=409)
    print(f"  beta address exhaustion rejected: {err['detail']}\n")

    # revoke alpha: active session terminates and the device token no longer works
    call("POST", f"/api/v1/tenants/{tenant_id}/devices/{devices[0][0]}/revoke",
         admin_key=admin_key, body={}, expect=200)
    call("POST", f"/api/v1/tenants/{tenant_id}/devices/{devices[0][0]}/revoke",
         admin_key=admin_key, body={}, expect=200)  # idempotent
    call("POST", "/api/v1/sessions", device_token=devices[0][1],
         body={"access_point_id": ap_id}, expect=403)
    print("  alpha revoked; revoked token cannot reconnect (403)\n")

    _, leases = call("GET", f"/api/v1/tenants/{tenant_id}/leases", admin_key=admin_key, expect=200)
    assert any(l["close_reason"] == "revoked" and l["closed_at"] for l in leases), leases
    print(f"  admin audit view: {len(leases)} lease(s), revoke recorded with reason + timestamp\n")

    # device token must never drive admin endpoints
    call("GET", f"/api/v1/tenants/{tenant_id}/devices", device_token=devices[1][1], expect=403)
    print("  device token has no admin rights (403)\n")

    print("== demo complete ==")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as exc:
        print(f"cannot reach {BASE_URL}: {exc}", file=sys.stderr)
        print("start the stack first: sudo docker compose up --build", file=sys.stderr)
        sys.exit(1)
