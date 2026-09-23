#!/usr/bin/env python3
"""Acceptance smoke driver against a running forkindexer HTTP server."""
import json
import sys
import urllib.request
import urllib.error

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:39081"
PATH = sys.argv[2] if len(sys.argv) > 2 else "examples/stream1_three_forks.ndjson"


def call(method, url, body=None, expect_status=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            payload = json.loads(r.read())
            status = r.status
    except urllib.error.HTTPError as e:
        payload = json.loads(e.read())
        status = e.code
    if expect_status is not None and status != expect_status:
        raise AssertionError(f"{method} {url}: expected {expect_status}, got {status}: {payload}")
    return status, payload


lines = [json.loads(l) for l in open(PATH) if l.strip() and not l.startswith(("//", "#"))]

def head():
    _, d = call("GET", f"{BASE}/v1/chain/head")
    return d

# 1..4 : G A1 B1 A2 -> head is A2 (weight 3); tie between A2 and nothing yet
for env in lines[:4]:
    call("POST", f"{BASE}/v1/blocks", env, 200)
h = head()
assert h["height"] == 2 and h["cum_weight"] == 3, h
assert h["head"] == lines[3]["block"]["hash"], h
print("after seq4 head=A2 height2 weight3 cursor", h["cursor"])

# 5: orphan B3 staged, head unchanged
st, res = call("POST", f"{BASE}/v1/blocks", lines[4], 200)
assert res["block_status"] == "staged", res
assert head()["head"] == lines[3]["block"]["hash"]
print("seq5 orphan B3 staged")

# 6: B2 arrives -> cascade B3 connects (weight4) -> reorg to B3
st, res = call("POST", f"{BASE}/v1/blocks", lines[5], 200)
assert res["head_after"] == lines[4]["block"]["hash"], res
assert res["reorg"] is True, res
h = head()
assert h["height"] == 3 and h["cum_weight"] == 4, h
print("seq6 B2 -> cascade connects B3, reorg head=B3 weight4")

# balances now reflect B chain; A chain credits gone
def balance(a):
    _, d = call("GET", f"{BASE}/v1/addresses/{a}/balance")
    return d["balance"]
assert balance("0x" + "a"*40) == -1195
assert balance("0x" + "b"*40) == 1160
assert balance("0x" + "c"*40) == 0
assert balance("0x" + "d"*40) == 35
print("balances after reorg OK (alice -1195, bob 1160, carol 0, dave 35)")

# 7: C2 third fork at weight 3 cannot beat weight 4, stays out
st, res = call("POST", f"{BASE}/v1/blocks", lines[6], 200)
assert res["reorg"] is False, res
assert head()["head"] == lines[4]["block"]["hash"]
print("seq7 C2 third fork does not beat B3")

# A2 must now be flagged off-chain but still queryable
_, b = call("GET", f"{BASE}/v1/blocks/" + lines[3]["block"]["hash"])
assert b["status"] == "connected" and b["in_chain"] is False, b
# B3 on chain
_, b = call("GET", f"{BASE}/v1/blocks/" + lines[4]["block"]["hash"])
assert b["in_chain"] is True, b
print("off-chain A2 still stored; B3 in_chain")

# replay ALL envelopes: idempotent, cursor unchanged, balances identical
for env in lines:
    st, res = call("POST", f"{BASE}/v1/blocks", env, 200)
    assert res["already_known"] is True, res
assert head()["cursor"] == 7
assert balance("0x" + "a"*40) == -1195 and balance("0x" + "b"*40) == 1160
print("full replay idempotent, cursor stays 7, no double counting")

# sequence gap must be rejected
gap = {"sequence": 99, "block": lines[0]["block"]}
st, res = call("POST", f"{BASE}/v1/blocks", gap, 409)
print("gap rejected:", res["code"])

# hash mismatch
bad = json.load(open("examples/bad_hash_mismatch.json"))
st, res = call("POST", f"{BASE}/v1/blocks", bad, 422)
print("hash mismatch rejected:", res["code"])

# same hash different content: through the HTTP boundary the real SHA-256
# check catches the tampered body first (a forger cannot produce a matching
# hash). The raw same-hash/different-content defense is covered at the store
# level in Go tests.
bad2 = json.load(open("examples/bad_same_hash_different_content.json"))
bad2["sequence"] = 8
st, res = call("POST", f"{BASE}/v1/blocks", bad2, 422)
assert res["code"] == "hash_mismatch", res
print("forged body reusing known hash rejected:", res["code"])

# unknown block 404
st, _ = call("GET", f"{BASE}/v1/blocks/0x" + "f"*64, 404)
print("unknown block -> 404")

# verify endpoint
st, rep = call("GET", f"{BASE}/v1/verify", 200)
assert rep["ok"] is True, rep
print("verify-from-genesis OK")

print("ALL SMOKE CHECKS PASSED")
