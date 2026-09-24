"""Acceptance queries driven over the running HTTP API.

Usage: python scripts/acceptance_queries.py http://127.0.0.1:8791

Every assertion is computed by the server from a real uploaded snapshot;
the script exits non-zero on the first mismatch.
"""

from __future__ import annotations

import copy
import json
import sys
import urllib.error
import urllib.request


def call(base: str, method: str, path: str, body=None, expect_status=200):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            status, payload = resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, json.load(exc)
    assert status == expect_status, (
        f"{method} {path}: expected {expect_status}, got {status}: {payload}"
    )
    return payload


def main(base: str) -> int:
    doc = json.load(open("examples/scenario.json"))
    snapshot = doc["snapshot"]

    print("-- upload immutable snapshot")
    created = call(base, "POST", "/api/v1/snapshots", snapshot)
    sid = created["snapshotId"]
    assert len(sid) == 64, f"snapshot id is not a SHA-256 hex digest: {sid}"
    assert any("SCTP" in w for w in created["warnings"]), created["warnings"]

    reuploaded = call(base, "POST", "/api/v1/snapshots", copy.deepcopy(snapshot))
    assert reuploaded["snapshotId"] == sid and reuploaded["created"] is False
    print(f"   snapshot id (SHA-256): {sid}")

    checks = [
        ("named port web->api 9090 allowed",
         {"source": {"pod": {"namespace": "frontend", "name": "web-1"}},
          "destination": {"pod": {"namespace": "backend", "name": "api-1"}},
          "protocol": "TCP", "port": 9090}, True),
        ("out-of-range port 9201 denied by ingress",
         {"source": {"pod": {"namespace": "frontend", "name": "web-1"}},
          "destination": {"pod": {"namespace": "backend", "name": "api-1"}},
          "protocol": "TCP", "port": 9201}, False),
        ("named pg port api->db allowed",
         {"source": {"pod": {"namespace": "backend", "name": "api-1"}},
          "destination": {"pod": {"namespace": "database", "name": "db-1"}},
          "protocol": "TCP", "port": 5432}, True),
        ("unselected evil pod default-denied at db",
         {"source": {"pod": {"namespace": "backend", "name": "evil-1"}},
          "destination": {"pod": {"namespace": "database", "name": "db-1"}},
          "protocol": "TCP", "port": 5432}, False),
        ("ipBlock permits 10.2.44.20",
         {"source": {"pod": {"namespace": "backend", "name": "api-1"}},
          "destination": {"ip": "10.2.44.20"},
          "protocol": "TCP", "port": 443}, True),
        ("ipBlock except excludes 10.2.44.10",
         {"source": {"pod": {"namespace": "backend", "name": "api-1"}},
          "destination": {"ip": "10.2.44.10"},
          "protocol": "TCP", "port": 443}, False),
        ("UDP/5432 distinct from TCP/5432 denied",
         {"source": {"pod": {"namespace": "backend", "name": "api-1"}},
          "destination": {"pod": {"namespace": "database", "name": "db-1"}},
          "protocol": "UDP", "port": 5432}, False),
    ]

    for label, query, expected in checks:
        r = call(base, "POST", f"/api/v1/snapshots/{sid}/analyze", query)
        assert r["reachable"] is expected, (
            f"{label}: expected reachable={expected}, got {r['reachable']} "
            f"({r['reason']})"
        )
        print(f"   [{'PASS' if r['reachable'] == expected else 'FAIL'}] {label}")

    print("-- evidence is returned for an allowed flow")
    r = call(base, "POST", f"/api/v1/snapshots/{sid}/analyze", checks[0][1])
    ev = r["ingress"]["allowedBy"]
    assert ev and ev[0]["policy"]["name"] == "api-ingress"
    assert ev[0]["allowedPorts"] == "named:grpc"
    print(f"   matched: {ev[0]['policy']['namespace']}/{ev[0]['policy']['name']} "
          f"rule {ev[0]['ruleIndex']} peer {ev[0]['peerIndex']}")

    print("-- unknown protocol is reported unsupported (HTTP 422)")
    bad = call(base, "POST", f"/api/v1/snapshots/{sid}/analyze",
               {"source": {"pod": {"namespace": "frontend", "name": "web-1"}},
                "destination": {"pod": {"namespace": "backend", "name": "api-1"}},
                "protocol": "SCTP", "port": 5000}, expect_status=422)
    assert bad["error"] == "unsupported_protocol"
    print("   SCTP -> 422 unsupported_protocol")

    print("-- namespace label change revokes reachability")
    changed = copy.deepcopy(snapshot)
    for ns_obj in changed["namespaces"]:
        if ns_obj["name"] == "frontend":
            ns_obj["labels"]["tier"] = "former-web"
    sid2 = call(base, "POST", "/api/v1/snapshots", changed)["snapshotId"]
    r = call(base, "POST", f"/api/v1/snapshots/{sid2}/analyze", checks[0][1])
    assert r["reachable"] is False, r
    print("   frontend tier label changed -> web->api now denied")

    print("-- unknown snapshot -> 404")
    call(base, "GET", "/api/v1/snapshots/does-not-exist", expect_status=404)

    return 0


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8791"
    raise SystemExit(main(base.rstrip("/")))
