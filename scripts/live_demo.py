"""Live acceptance demo against a RUNNING server (started by acceptance.sh).

Exercises the full system over real HTTP with genuine Ed25519 signatures:
  1. registration + epoch-frozen snapshots
  2. double sign arriving in REVERSE order -> one canonical evidence id
  3. boundary rounds (9 -> epoch 0, 10 -> epoch 1) use different snapshots
  4. invalid signature / cross-chain replay / tampered body rejected
  5. identical repeat never punished
  6. concurrent merge of the same offense -> exactly one penalty
"""
from __future__ import annotations

import os
import sys
import threading

import httpx

from app.testsign import make_vote, new_validator

BASE = os.getenv("EPE_BASE_URL", "http://127.0.0.1:8000")
CHAIN = "acceptance-chain"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=10, trust_env=False)
    c.get("/health").raise_for_status()
    print(f"connected to {BASE}")

    v = new_validator()

    print("\n== setup ==")
    r = c.post("/chains", json={"chain_id": CHAIN})
    check("register chain", r.status_code == 201)
    r = c.post("/validators", json={
        "chain_id": CHAIN, "public_key": v["public_key"], "moniker": "acc-1"})
    check("register validator", r.status_code == 201)
    addr = r.json()["validator_address"]
    for epoch, power in ((0, 1_000_000), (1, 4_000_000)):
        r = c.post("/snapshots", json={
            "chain_id": CHAIN, "validator_address": addr,
            "epoch": epoch, "voting_power": power})
        check(f"freeze epoch {epoch} snapshot @ {power}", r.status_code == 201)

    print("\n== reverse arrival order, same evidence id ==")
    va = make_vote(v, chain_id=CHAIN, height=42, round=3,
                   vote_type="prevote", block_hash="aa" * 32)
    vb = make_vote(v, chain_id=CHAIN, height=42, round=3,
                   vote_type="prevote", block_hash="bb" * 32)
    # vb first, then va (reverse)
    r1 = c.post("/votes", json=vb).json()
    r2 = c.post("/votes", json=va).json()
    check("first vote no conflict", r1["status"] == "accepted_no_conflict")
    check("reverse-order second vote triggers penalty", r2["status"] == "penalized")
    eid = r2["evidence_id"]
    r3 = c.post("/votes", json=vb).json()
    check("late repeat already penalized", r3["status"] == "already_penalized")
    check("evidence id stable", r3["evidence_id"] == eid)
    detail = c.get(f"/evidence/{eid}").json()
    check("raw evidence preserves two original votes",
          len(detail["raw_evidence"]["votes"]) == 2)
    check("judgment version stored", detail["judgment_version"] == "rules-1.0.0")
    check("1% slash of 1,000,000 = 10,000",
          detail["penalty"]["slashed_power"] == 10_000)

    print("\n== boundary rounds ==")
    def boundary(round_index, expected_epoch, expected_power, expected_slash):
        hashes = ("c1" * 32, "c2" * 32)
        for bh in hashes:
            c.post("/votes", json=make_vote(
                v, chain_id=CHAIN, height=100, round=round_index,
                vote_type="precommit", block_hash=bh))
        ev = [e for e in c.get("/evidence").json()["evidence"]
              if e["round"] == round_index][0]
        check(f"round {round_index} -> epoch {expected_epoch}", ev["epoch"] == expected_epoch)
        p = c.get(f"/evidence/{ev['evidence_id']}").json()["penalty"]
        check(f"round {round_index} frozen base {expected_power}",
              p["frozen_power"] == expected_power and p["slashed_power"] == expected_slash)

    boundary(9, 0, 1_000_000, 10_000)
    boundary(10, 1, 4_000_000, 40_000)

    print("\n== rejected inputs ==")
    bad_sig = dict(make_vote(v, chain_id=CHAIN, height=7, round=0, vote_type="prevote"))
    bad_sig["signature"] = "00" * 64
    check("invalid signature -> 422", c.post("/votes", json=bad_sig).status_code == 422)

    # Sign legitimately on CHAIN, then replay the exact signed bytes on
    # another registered chain without re-signing -> signature must fail.
    c.post("/chains", json={"chain_id": "other-chain"})
    c.post("/validators", json={"chain_id": "other-chain", "public_key": v["public_key"]})
    cross = make_vote(v, chain_id=CHAIN, height=7, round=0, vote_type="prevote")
    cross["chain_id"] = "other-chain"
    check("cross-chain replay -> 422", c.post("/votes", json=cross).status_code == 422)

    tampered = make_vote(v, chain_id=CHAIN, height=7, round=1,
                         vote_type="prevote", block_hash="dd" * 32)
    tampered["block_hash"] = "ee" * 32
    check("tampered block hash -> 422", c.post("/votes", json=tampered).status_code == 422)

    print("\n== identical repeat is not a double sign ==")
    dup = make_vote(v, chain_id=CHAIN, height=200, round=0,
                    vote_type="prevote", block_hash="77" * 32)
    s1 = c.post("/votes", json=dup).json()["status"]
    s2 = c.post("/votes", json=dup).json()["status"]
    check(f"first={s1}, repeat={s2}",
          s1 == "accepted_no_conflict" and s2 == "duplicate_vote")

    print("\n== concurrent merge: 16 parallel conflicting deliveries ==")
    pair = [
        make_vote(v, chain_id=CHAIN, height=300, round=5,
                  vote_type="prevote", block_hash=bh)
        for bh in ("f1" * 32, "f2" * 32)
    ]
    outcomes: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(16)

    def hit(i):
        barrier.wait()
        s = c.post("/votes", json=pair[i % 2]).json()["status"]
        with lock:
            outcomes.append(s)

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    penalized = outcomes.count("penalized")
    evidence_for_300 = [e for e in c.get("/evidence").json()["evidence"]
                       if e["height"] == 300]
    penalties_for_300 = [p for p in c.get("/penalties").json()["penalties"]
                         if p["chain_id"] == CHAIN]
    check("exactly one 'penalized' outcome", penalized == 1, str(outcomes))
    check("exactly one evidence row", len(evidence_for_300) == 1)

    print("\n== summary ==")
    total_penalties = len(c.get("/penalties").json()["penalties"])
    print(f"total penalties on chain: {total_penalties}")
    if failures:
        print(f"\n{len(failures)} CHECK(S) FAILED: {failures}")
        return 1
    print("\nALL LIVE ACCEPTANCE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
