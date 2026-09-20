#!/usr/bin/env python3
"""End-to-end CloudGate control-plane demo over HTTP.

Creates an isolated demo tenant (unique name per run) and exercises:
  * pool creation with excluded network/broadcast/reserved addresses
  * address exhaustion + AP capacity competition
  * idempotent connect
  * one-active-session-per-device + generation bumping on reconnect
  * late heartbeat rejected on the old generation
  * revoke: session terminated, old device token unusable
  * termination audit records

Usage:  python scripts/demo.py --base-url http://localhost:8000
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.request

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
BOLD = "\033[1m"
RESET = "\033[0m"


class Client:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def request(self, method: str, path: str, token: str | None = None,
                device_token: str | None = None, lease_token: str | None = None,
                body: dict | None = None):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if device_token:
            headers["X-Device-Token"] = device_token
        if lease_token:
            headers["X-Lease-Token"] = lease_token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode()
            try:
                return exc.code, json.loads(payload)
            except json.JSONDecodeError:
                return exc.code, {"raw": payload}


def step(title: str) -> None:
    print(f"\n{BOLD}{CYAN}== {title} =={RESET}")


def ok(label: str, condition: bool, detail: str = "") -> None:
    mark = f"{GREEN}PASS{RESET}" if condition else f"{RED}FAIL{RESET}"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        ok.failures += 1
ok.failures = 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--platform-user", default="admin")
    parser.add_argument("--platform-pass", default="admin12345")
    args = parser.parse_args()
    c = Client(args.base_url)

    run = secrets.token_hex(3)
    tenant_name = f"demo-{run}"
    tenant_admin_user = f"admin-{run}"
    tenant_admin_pass = "tenant-pass-123"

    step("0. health")
    status, body = c.request("GET", "/health")
    ok("service healthy", status == 200, str(body))
    timeout = body.get("heartbeat_timeout_seconds")

    step("1. platform admin login + create tenant & tenant admin")
    status, body = c.request("POST", "/api/admin/login",
                             body={"username": args.platform_user, "password": args.platform_pass})
    ok("platform admin login", status == 200)
    platform = body["access_token"]

    status, body = c.request("POST", "/api/platform/tenants", token=platform, body={"name": tenant_name})
    ok("tenant created", status == 201, body.get("name", ""))
    tenant_id = body["id"]

    status, body = c.request(
        "POST", f"/api/platform/tenants/{tenant_id}/admins", token=platform,
        body={"username": tenant_admin_user, "password": tenant_admin_pass},
    )
    ok("tenant admin created", status == 201, str(body))

    status, body = c.request("POST", "/api/admin/login",
                             body={"username": tenant_admin_user, "password": tenant_admin_pass})
    ok("tenant admin login", status == 200)
    admin = body["access_token"]

    t = f"/api/tenants/{tenant_id}"

    step("2. create address pool /29 with reserved gateway")
    # 10.20.30.0/29 -> hosts .1.. .6; reserve .1 -> 5 usable, .0 network/.7 broadcast excluded
    status, body = c.request("POST", f"{t}/pools", token=admin,
                             body={"name": "p1", "cidr": "10.20.30.0/29",
                                   "reserved_addresses": ["10.20.30.1"]})
    ok("pool created, exclusions counted", status == 201,
       f"usable={body.get('usable_addresses')} reserved={body.get('reserved_addresses')} total={body.get('total_addresses')}")
    ok("network+broadcast excluded and 1 reserved (5 usable of 8)",
       body.get("usable_addresses") == 5 and body.get("reserved_addresses") == 1
       and body.get("total_addresses") == 8)
    pool_id = body["id"]

    step("3. access point with capacity 3")
    status, body = c.request("POST", f"{t}/access-points", token=admin,
                             body={"name": "ap1", "pool_id": pool_id, "capacity": 3})
    ok("AP created", status == 201, str(body))
    ap_id = body["id"]

    devices: list[tuple[str, str]] = []
    for i in range(4):
        status, body = c.request("POST", f"{t}/devices", token=admin, body={"name": f"dev{i}"})
        assert status == 201, body
        devices.append((body["id"], body["device_token"]))

    step("4. address exhaustion + capacity competition (4 devices, capacity 3, 5 addresses)")
    leases: list[dict] = []
    for i, (did, dtok) in enumerate(devices):
        status, body = c.request("POST", "/api/device/connect", device_token=dtok,
                                 body={"access_point_id": ap_id, "idempotency_key": f"k{i}"})
        if i < 3:
            ok(f"device {i} connected", status == 201, body.get("session", {}).get("ip", ""))
            leases.append(body)
        else:
            ok(f"device {i} rejected: capacity", status == 503,
               body.get("error", {}).get("code", ""))

    ips = sorted(l["session"]["ip"] for l in leases)
    ok("all allocated IPs unique", len(set(ips)) == 3, str(ips))
    ok("never allocated .0/.7/.1", all(ip not in ("10.20.30.0", "10.20.30.7", "10.20.30.1") for ip in ips),
       str(ips))

    step("5. idempotent replay")
    did, dtok = devices[0]
    status, body = c.request("POST", "/api/device/connect", device_token=dtok,
                             body={"access_point_id": ap_id, "idempotency_key": "k0"})
    ok("same idempotency key reuses lease", status == 201 and body["idempotent_reused"] is True)
    ok("same IP returned", body["session"]["ip"] == leases[0]["session"]["ip"])

    step("6. heartbeat keeps the lease alive; wrong generation rejected")
    l0 = leases[0]
    status, body = c.request(
        "POST", "/api/device/heartbeat", device_token=dtok, lease_token=l0["lease_token"],
        body={"session_id": l0["session"]["id"], "generation": l0["session"]["generation"]},
    )
    ok("valid heartbeat accepted", status == 200)
    status, body = c.request(
        "POST", "/api/device/heartbeat", device_token=dtok, lease_token=l0["lease_token"],
        body={"session_id": l0["session"]["id"], "generation": l0["session"]["generation"] + 1},
    )
    ok("wrong generation heartbeat rejected (stale_generation)", status == 409,
       body.get("error", {}).get("code", ""))

    step("7. reconnect bumps generation; old lease token can't touch new session")
    status, body = c.request("POST", "/api/device/connect", device_token=dtok,
                             body={"access_point_id": ap_id, "idempotency_key": f"reconnect-{run}"})
    ok("reconnect succeeds", status == 201, f"gen {body['session']['generation']}")
    ok("generation incremented", body["session"]["generation"] == l0["session"]["generation"] + 1)
    new_lease = body
    status, late = c.request(
        "POST", "/api/device/heartbeat", device_token=dtok, lease_token=l0["lease_token"],
        body={"session_id": l0["session"]["id"], "generation": l0["session"]["generation"]},
    )
    ok("late heartbeat for OLD generation rejected", status == 409,
       late.get("error", {}).get("code", ""))
    status, body = c.request(
        "POST", "/api/device/heartbeat", device_token=dtok, lease_token=new_lease["lease_token"],
        body={"session_id": new_lease["session"]["id"], "generation": new_lease["session"]["generation"]},
    )
    ok("new generation heartbeat works", status == 200)
    # Old IP freed, new IP distinct
    ok("new IP differs from old (old released)", new_lease["session"]["ip"] != l0["session"]["ip"])

    step("8. admin sees termination record for the superseded lease")
    status, body = c.request(
        "GET", f"{t}/sessions/{l0['session']['id']}/termination", token=admin,
    )
    ok("reconnect termination recorded with reason+time", status == 200
       and body["reason"] == "reconnect" and body["terminated_at"], str(body))

    step("9. revoke terminates lease and bricks the old device token")
    # free capacity check: reconnect consumed device0's slot, still 3 sessions active overall
    status, body = c.request("POST", f"{t}/devices/{did}/revoke", token=admin, body={})
    ok("device revoked", status == 200 and body["revoked"] is True)
    status, body = c.request("POST", "/api/device/connect", device_token=dtok,
                             body={"access_point_id": ap_id, "idempotency_key": "after-revoke"})
    ok("old device token cannot reconnect", status in (401, 403), body.get("error", {}).get("code", ""))
    status, body = c.request(
        "GET", f"{t}/sessions/{new_lease['session']['id']}/termination", token=admin,
    )
    ok("active lease terminated with reason=revoked", status == 200 and body["reason"] == "revoked")

    # A previously full AP should now have a free slot (device0 gone).
    did3, dtok3 = devices[3]
    status, body = c.request("POST", "/api/device/connect", device_token=dtok3,
                             body={"access_point_id": ap_id, "idempotency_key": "k3"})
    ok("freed slot reusable by another device", status == 201, body.get("session", {}).get("ip", ""))

    step("10. cross-tenant isolation")
    status, body = c.request("POST", "/api/platform/tenants", token=platform, body={"name": f"other-{run}"})
    other_tenant = body["id"]
    status, body = c.request("GET", f"/api/tenants/{other_tenant}/sessions", token=admin)
    ok("tenant admin cannot read another tenant (404, not 403/200)", status == 404)
    status, body = c.request("GET", f"{t}/sessions", token=admin)
    ok("own tenant sessions visible", status == 200 and isinstance(body, list) and len(body) >= 3)

    print(f"\n{BOLD}heartbeat timeout is {timeout}s — expiry/recovery is covered by the test suite "
          f"(tests run with a 2s timeout).{RESET}")

    if ok.failures:
        print(f"\n{RED}{BOLD}{ok.failures} check(s) failed{RESET}")
        return 1
    print(f"\n{GREEN}{BOLD}all demo checks passed{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
