#!/usr/bin/env python3
"""End-to-end acceptance demo for the incremental path repair service.

Uses only the Python standard library for HTTP, so it works against a
server started from the locked venv without extra setup.

Flow:
  1. build the map from examples/create_map.json
  2. initial plan (D* Lite cost is checked against independent Dijkstra)
  3. local cost updates
  4. add a full wall  -> goal becomes unreachable
  5. open a gap       -> path repaired incrementally
  6. move the start
  7. show that a stale snapshot id is rejected (HTTP 409)
  8. verify the HMAC signature of a planning response

Usage:
    python examples/demo.py [base_url]
default base_url: http://127.0.0.1:8000
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from urllib import error, request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
HERE = Path(__file__).resolve().parent


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def show_plan(title: str, body: dict) -> None:
    print(f"\n=== {title} ===")
    if not body.get("reachable"):
        print("  goal UNREACHABLE (Dijkstra agrees: "
              f"{body.get('dijkstra_cost') is None})")
    else:
        print(f"  D* Lite cost : {body['cost']}")
        print(f"  Dijkstra cost: {body['dijkstra_cost']}")
        print(f"  optimal match: {body['optimal_match']}")
        print(f"  path length  : {len(body['path'])} cells")
        print(f"  path walkable: {body['path_valid']} "
              f"(independent sum = {body['path_cost_check']})")
    print(f"  D* Lite re-expanded nodes : {body['reexpanded_nodes']}  "
          "(diagnostic, not a performance guarantee)")
    print(f"  Dijkstra settled nodes    : {body['dijkstra_settled_nodes']}")
    print(f"  vertex updates            : {body['vertex_updates']}")
    print(f"  revision/snapshot         : {body['revision']} / {body['snapshot_id'][:12]}…")


def main() -> int:
    map_doc = json.loads((HERE / "create_map.json").read_text())
    updates_doc = json.loads((HERE / "cost_updates.json").read_text())

    status, m = call("POST", "/maps", map_doc)
    assert status == 201, m
    mid, snap, key_hex = m["map_id"], m["snapshot_id"], m["hmac_key"]
    print(f"created map {mid}")
    print(f"snapshot (SHA-256) = {snap}")
    print(f"hmac key (32 bytes, hex) = {key_hex}")

    def plan(label, snapshot):
        st, body = call("POST", f"/maps/{mid}/plan",
                        {"map_id": mid, "expected_snapshot_id": snapshot})
        assert st == 200, body
        show_plan(label, body)
        return body

    first = plan("1. initial plan", snap)
    assert first["optimal_match"]

    st, body = call("POST", f"/maps/{mid}/costs",
                    {"map_id": mid, "expected_snapshot_id": snap,
                     "updates": updates_doc})
    assert st == 200, body
    snap = body["snapshot_id"]
    show_plan("2. after local cost updates (auto replan)", body)
    assert body["optimal_match"]

    # Seal the corridor completely: wall across the whole width at x=5.
    wall = [{"x": 5, "y": y, "blocked": True} for y in range(m["height"])]
    st, body = call("POST", f"/maps/{mid}/costs",
                    {"map_id": mid, "expected_snapshot_id": snap, "updates": wall})
    assert st == 200, body
    snap = body["snapshot_id"]
    show_plan("3. wall added across x=5 -> unreachable", body)
    assert body["reachable"] is False and body["optimal_match"]

    # Reopen a single gap at (5, 3).
    st, body = call("POST", f"/maps/{mid}/costs",
                    {"map_id": mid, "expected_snapshot_id": snap,
                     "updates": [{"x": 5, "y": 3, "blocked": False, "cost": 1.0}]})
    assert st == 200, body
    snap = body["snapshot_id"]
    show_plan("4. gap reopened at (5,3) -> incremental repair", body)
    assert body["reachable"] and body["optimal_match"]

    # Move the start far away.
    st, body = call("POST", f"/maps/{mid}/move-start",
                    {"map_id": mid, "expected_snapshot_id": snap,
                     "start": [0, 0]})
    assert st == 200, body
    snap = body["snapshot_id"]
    show_plan("5. start moved to (0,0)", body)
    assert body["optimal_match"]

    # A stale snapshot must be rejected: no reuse of dead paths.
    old_snap = first["snapshot_id"]
    st, body = call("POST", f"/maps/{mid}/plan",
                    {"map_id": mid, "expected_snapshot_id": old_snap})
    print(f"\n6. plan against stale snapshot -> HTTP {st} ({body.get('error')})")
    assert st == 409 and body["error"] == "snapshot_mismatch"

    # Verify the signature of the last good response, then tamper it.
    st, signed = call("POST", f"/maps/{mid}/plan",
                      {"map_id": mid, "expected_snapshot_id": snap})
    sig = signed.pop("signature")
    canon = json.dumps(signed, sort_keys=True, separators=(",", ":")).encode()
    expect = hmac.new(bytes.fromhex(key_hex),
                      snap.encode() + b"." + canon, hashlib.sha256).hexdigest()
    print(f"\n7. HMAC verification: recomputed matches server = "
          f"{hmac.compare_digest(expect, sig)}")
    assert hmac.compare_digest(expect, sig)
    st, body = call("POST", f"/maps/{mid}/verify",
                    {"map_id": mid, "snapshot_id": snap,
                     "body": signed, "signature": sig})
    assert st == 200 and body["valid"] is True, body
    signed["cost"] = 0.01
    st, body = call("POST", f"/maps/{mid}/verify",
                    {"map_id": mid, "snapshot_id": snap,
                     "body": signed, "signature": sig})
    print(f"   tampered body rejected by server = {body['valid'] is False}")
    assert body["valid"] is False

    call("DELETE", f"/maps/{mid}")
    print("\nALL ACCEPTANCE STEPS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
