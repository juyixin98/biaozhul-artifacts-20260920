#!/usr/bin/env python3
"""End-to-end demo (no network framework; drives the service in-process).

Shows:
  1. build a 7-leaf log (RFC 9162 example shape, non-power-of-two)
  2. fetch a signed tree head (STH) and verify the Ed25519 signature
  3. fetch and locally verify inclusion proofs for every leaf
  4. append more leaves and verify a consistency proof between heads
  5. show that a forged root / wrong index / tampered proof is rejected

Run:  python3 examples/demo.py
"""

import base64
import sys
import tempfile
from pathlib import Path

# Allow running directly: python3 examples/demo.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tl import merkle
from tl.log import TransparentLog
from tl.signing import KeyManager, verify_sth_signature
from tl.store import LogStore


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="tl-demo-")
    store = LogStore(f"{tmp}/log.jsonl")
    keys = KeyManager(f"{tmp}/ed25519_test_key.bin")
    tlog = TransparentLog(store, keys)

    print("== 1. append 7 entries ==")
    for i in range(7):
        idx = tlog.add(f"entry-{i}".encode())
        print(f"  appended index {idx}: leaf={merkle.leaf_hash(f'entry-{i}'.encode()).hex()[:16]}...")

    print("\n== 2. signed tree head ==")
    sth = tlog.current_sth()
    print(f"  tree_size   = {sth.tree_size}")
    print(f"  root_hash   = {sth.root_hash.hex()}")
    print(f"  timestamp_us= {sth.timestamp_us}")
    print(f"  signature   = {sth.signature.hex()[:32]}...")
    ok, reason = verify_sth_signature(
        keys.public_key_raw(), sth.tree_size, sth.root_hash,
        sth.timestamp_us, sth.signature,
    )
    print(f"  signature verifies: {ok} ({reason})")

    print("\n== 3. inclusion proofs, verified locally ==")
    for idx in range(7):
        r = tlog.inclusion_by_index(idx)
        ok = merkle.verify_inclusion(
            r.leaf_index, r.tree_size, r.leaf_hash, r.proof, r.root_hash
        )
        print(f"  leaf {idx}: proof_len={len(r.proof)} verified={ok}")

    print("\n== 4. append 5 more; consistency 7 -> 12 (non-power-of-two old size) ==")
    for i in range(7, 12):
        tlog.add(f"entry-{i}".encode())
    old_root = tlog.store.root_at(7)
    new_sth = tlog.current_sth()
    con = tlog.consistency(7, 12)
    ok = merkle.verify_consistency(
        con.old_size, con.new_size, con.old_root, con.new_root, con.proof
    )
    print(f"  consistency proof len={len(con.proof)} verified={ok}")
    print(f"  old root (size 7)  = {old_root.hex()}")
    print(f"  new root (size 12) = {new_sth.root_hash.hex()}")

    print("\n== 5. attacker attempts (all must be rejected) ==")
    r = tlog.inclusion_by_index(3, tree_size=7)
    forged_root = b"\x00" * 32
    attempts = [
        ("forged root",
         merkle.verify_inclusion(3, 7, r.leaf_hash, r.proof, forged_root)),
        ("wrong index (claim leaf at 4)",
         merkle.verify_inclusion(4, 7, r.leaf_hash, r.proof, r.root_hash)),
        ("flipped bit in proof[0]",
         merkle.verify_inclusion(
             3, 7, r.leaf_hash,
             [bytes([r.proof[0][0] ^ 0xFF]) + r.proof[0][1:]] + r.proof[1:],
             r.root_hash)),
        ("truncated proof",
         merkle.verify_inclusion(3, 7, r.leaf_hash, r.proof[:-1], r.root_hash)),
        ("forged old root in consistency",
         merkle.verify_consistency(7, 12, forged_root, con.new_root, con.proof)),
    ]
    for name, accepted in attempts:
        verdict = "ACCEPTED (BAD!)" if accepted else "rejected (good)"
        print(f"  {name:38s} -> {verdict}")

    print("\n== scope ==")
    print("  This service proves append-only consistency *of one log*.")
    print("  It does NOT prevent two colluding logs from presenting two")
    print("  different histories to different clients (no gossip/quorum).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
