#!/usr/bin/env python3
"""Independent Merkle proof verifier — Python, stdlib only (hashlib + json).

This is a third re-derivation of the verification spec (the service's Rust
crate and examples/verify.rs being the first two). It needs nothing from the
service except the proof JSON and the root the client already pinned.

Usage:
    python3 scripts/verify.py <proof.json|-> <root_hex> <query_key_hex>

Exit code 0 = verified, 1 = rejected (reason on stderr), 2 = usage error.
"""
import hashlib
import json
import sys


def h(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def leaf_hash(k: bytes, v: bytes) -> bytes:
    return h(b"\x00" + len(k).to_bytes(4, "big") + k + len(v).to_bytes(4, "big") + v)


def inner_hash(l: bytes, r: bytes) -> bytes:
    assert len(l) == len(r) == 32
    return h(b"\x01" + l + r)


def commit_hash(version: int, top: bytes, n: int, height: int) -> bytes:
    return h(
        b"\x02"
        + version.to_bytes(8, "big")
        + top
        + n.to_bytes(8, "big")
        + height.to_bytes(4, "big")
    )


def height_of(n: int) -> int:
    hh = 0
    while n > 1:
        n = (n + 1) // 2
        hh += 1
    return hh


def replay(b, n, height):
    idx0 = b["index"]
    if n == 0:
        raise ValueError("branch for empty tree")
    if not 0 <= idx0 < n:
        raise ValueError(f"index {idx0} out of range {n}")
    if len(b["path"]) != height or height != height_of(n):
        raise ValueError("path length inconsistent with height/leaf_count")
    cur = leaf_hash(bytes.fromhex(b["key_hex"]), bytes.fromhex(b["value_hex"]))
    pos, count, idx = idx0, n, 0
    for level, step in enumerate(b["path"]):
        side = step["side"]
        sib = bytes.fromhex(step["hash"]) if step.get("hash") else None
        if side == "left":
            if pos % 2 == 0:
                raise ValueError(f"level {level}: left sibling at even position")
            cur = inner_hash(sib, cur)
            bit = 1
        elif side == "right":
            if pos % 2 != 0 or pos + 1 >= count:
                raise ValueError(f"level {level}: invalid right sibling")
            cur = inner_hash(cur, sib)
            bit = 0
        elif side == "promoted":
            if pos % 2 != 0 or pos + 1 != count or sib is not None:
                raise ValueError(f"level {level}: invalid promotion")
            bit = 0
        else:
            raise ValueError(f"unknown side {side}")
        idx |= bit << level
        pos //= 2
        count = (count + 1) // 2
    if count != 1 or pos != 0 or idx != idx0:
        raise ValueError("path does not converge to the claimed index")
    return idx, cur


def verify(doc: dict, trusted: bytes, query: bytes) -> str:
    version, n, height = doc["version"], doc["leaf_count"], doc["height"]
    top_field = bytes.fromhex(doc["top"])
    if bytes.fromhex(doc["root"]) != trusted:
        raise ValueError("proof root != trusted root")

    if doc["kind"] == "existence":
        if bytes.fromhex(doc["key_hex"]) != query:
            raise ValueError("leaf key != queried key")
        _, top = replay(doc, n, height)
        if top != top_field:
            raise ValueError("reconstructed top != proof top")
        if commit_hash(version, top, n, height) != trusted:
            raise ValueError("commit hash mismatch")
        return f"VERIFIED PRESENT version={version} value_hex={doc['value_hex']}"

    if doc["kind"] != "non_existence":
        raise ValueError(f"bad kind {doc['kind']!r}")

    if n == 0:
        if doc["prev"] is not None or doc["next"] is not None:
            raise ValueError("empty-tree proof carries neighbours")
        top = b"\x00" * 32
    else:
        top = None
        prev, nxt = doc.get("prev"), doc.get("next")
        if prev is None and nxt is None:
            raise ValueError("absence proof without neighbours in non-empty tree")
        if prev is not None:
            pk = bytes.fromhex(prev["key_hex"])
            if not (pk < query):
                raise ValueError("prev key not < query")
            i, t1 = replay(prev, n, height)
            top = t1
            if nxt is not None:
                nk = bytes.fromhex(nxt["key_hex"])
                if not (query < nk):
                    raise ValueError("next key not > query")
                j, t2 = replay(nxt, n, height)
                if t1 != t2:
                    raise ValueError("branches replay to different tops")
                if j != i + 1:
                    raise ValueError(f"non-adjacent neighbours {i},{j}")
            else:
                if i != n - 1:
                    raise ValueError(f"withheld successor: prev idx {i} != {n-1}")
        if nxt is not None:
            nk = bytes.fromhex(nxt["key_hex"])
            if not (query < nk):
                raise ValueError("next key not > query")
            j, t2 = replay(nxt, n, height)
            if top is None:
                if j != 0:
                    raise ValueError(f"withheld predecessor: next idx {j} != 0")
                top = t2

    if top != top_field:
        raise ValueError("reconstructed top != proof top")
    if commit_hash(version, top, n, height) != trusted:
        raise ValueError("commit hash mismatch")
    return f"VERIFIED ABSENT version={version}"


def main(argv):
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    path, root_hex, key_hex = argv
    raw = sys.stdin.buffer.read() if path == "-" else open(path, "rb").read()
    try:
        doc = json.loads(raw)
        trusted = bytes.fromhex(root_hex)
        query = bytes.fromhex(key_hex)
        assert len(trusted) == 32
        print(verify(doc, trusted, query))
        return 0
    except (ValueError, KeyError, AssertionError, json.JSONDecodeError) as e:
        print(f"REJECTED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
