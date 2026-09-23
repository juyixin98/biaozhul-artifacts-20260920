#!/usr/bin/env python3
"""Independent Merkle-proof verifier — third implementation, standard library only.

This script implements the proof verification protocol from scratch using
nothing but hashlib.sha256. It does not import or trust any code from the
service; it is written against the wire protocol documented in README.md so
it stands in for an external relying party.

Usage:
  # Verify a full proof response against a root (reads proof JSON from stdin):
  curl -s 'http://127.0.0.1:39173/v1/proofs/key/626f62' \
    | python3 examples/verify.py <ROOT_HEX>

  # Or hand it a saved {"root": "...", "response": {...}} envelope:
  python3 examples/verify.py --envelope proof.json

Exits 0 when valid, non-zero with a message otherwise.
"""

import hashlib
import json
import sys


EMPTY_ROOT = hashlib.sha256(b"\x02").digest()


def leaf_hash(k: bytes, v: bytes) -> bytes:
    pre = b"\x00" + len(k).to_bytes(4, "big") + k + len(v).to_bytes(4, "big") + v
    return hashlib.sha256(pre).digest()


def branch_hash(l: bytes, r: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + l + r).digest()


def level_width(n: int) -> int:
    return (n + 1) // 2


def verify_inclusion(p: dict, root: bytes) -> None:
    n = p["leaf_count"]
    idx = p["index"]
    path = p["path"]
    assert n > 0, "inclusion in empty tree"
    assert idx < n, "index out of range"

    depth = 0
    w = n
    while w > 1:
        w = level_width(w)
        depth += 1
    assert len(path) == depth, f"path depth {len(path)} != {depth}"

    # Index must be exactly what the side bits encode.
    derived = 0
    for i, step in enumerate(path):
        assert step["side"] in ("left", "right"), "bad side"
        if step["side"] == "left":  # path node is the right child
            derived |= 1 << i
    assert derived == idx, "index inconsistent with path sides"

    cur = leaf_hash(bytes.fromhex(p["entry"]["key"]), bytes.fromhex(p["entry"]["value"]))
    width = n
    slot = idx
    for level in range(depth):
        sib = bytes.fromhex(path[level]["sibling_hash"])
        assert len(sib) == 32
        if path[level]["side"] == "right":  # sibling to the right
            assert slot % 2 == 0, "side/parity mismatch"
            if slot + 1 == width:  # odd last node duplicated
                assert sib == cur, "duplicated-node sibling mismatch"
            cur = branch_hash(cur, sib)
        else:  # sibling to the left
            assert slot % 2 == 1, "side/parity mismatch"
            cur = branch_hash(sib, cur)
        slot //= 2
        width = level_width(width)

    assert cur == root, "root mismatch"


def verify_bound(b: dict, root: bytes) -> bytes:
    assert bytes.fromhex(b["proof"]["root"]) == root, "bound root differs"
    verify_inclusion(b["proof"], root)
    return bytes.fromhex(b["proof"]["entry"]["key"])


def verify_absence(p: dict, root: bytes) -> None:
    assert bytes.fromhex(p["root"]) == root, "embedded root mismatch"
    q = bytes.fromhex(p["queried_key"])

    if p["empty_tree"]:
        assert root == EMPTY_ROOT, "empty_tree flag but root is not empty root"
        assert not p.get("bounds"), "empty tree must have no bounds"
        return
    assert root != EMPTY_ROOT, "empty root without empty_tree flag"

    bounds = p["bounds"]
    if len(bounds) == 1:
        b = bounds[0]
        nk = verify_bound(b, root)
        if b["side"] == "left":  # greatest existing key, q above it
            assert nk < q, "left bound not smaller"
            assert b["proof"]["index"] + 1 == b["proof"]["leaf_count"], "not rightmost"
        elif b["side"] == "right":  # smallest existing key, q below it
            assert nk > q, "right bound not greater"
            assert b["proof"]["index"] == 0, "not leftmost"
        else:
            raise AssertionError("bad bound side")
    elif len(bounds) == 2:
        left = next(b for b in bounds if b["side"] == "left")
        right = next(b for b in bounds if b["side"] == "right")
        lk = verify_bound(left, root)
        rk = verify_bound(right, root)
        assert lk < q < rk, "query not strictly between bounds"
        assert left["proof"]["leaf_count"] == right["proof"]["leaf_count"], "count mismatch"
        assert left["proof"]["index"] + 1 == right["proof"]["index"], "bounds not adjacent"
    else:
        raise AssertionError(f"need 1 or 2 bounds, got {len(bounds)}")


def verify_response(resp: dict, root_hex: str) -> None:
    root = bytes.fromhex(root_hex)
    assert len(root) == 32
    if resp["exists"]:
        proof = resp["proof"]
        assert resp["key"] == proof["entry"]["key"], "response/proof key mismatch"
        assert resp["value"] == proof["entry"]["value"], "response/proof value mismatch"
        verify_inclusion(proof, root)
    else:
        assert resp["key"] == resp["proof"]["queried_key"], "response/proof key mismatch"
        verify_absence(resp["proof"], root)


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--envelope":
        with open(sys.argv[2], "rb") as f:
            envelope = json.load(f)
        root_hex = envelope["root"]
        resp = envelope["response"]
    elif len(sys.argv) == 2:
        root_hex = sys.argv[1].removeprefix("0x")
        resp = json.load(sys.stdin)
    else:
        print(__doc__)
        return 2

    try:
        verify_response(resp, root_hex)
    except AssertionError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    print("VALID")
    return 0


if __name__ == "__main__":
    sys.exit(main())
