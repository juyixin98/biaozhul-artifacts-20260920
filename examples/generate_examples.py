"""Generate example offline-vote payloads with REAL Ed25519 signatures.

Run:  .venv/bin/python -m examples.generate_examples

Writes deterministic JSON files into examples/generated/. Keys are derived from
fixed seeds via sha256, so re-running produces identical fixtures.
"""
from __future__ import annotations

import hashlib
import json
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import domain
from app.testsign import make_vote

OUT = os.path.join(os.path.dirname(__file__), "generated")


def validator(label: str) -> dict:
    seed = hashlib.sha256(label.encode()).digest()
    sk = Ed25519PrivateKey.from_private_bytes(seed)
    pub = sk.public_key().public_bytes_raw().hex()
    return {
        "private_key": sk,
        "public_key": pub,
        "validator_address": domain.derive_address(bytes.fromhex(pub)),
    }


def write(name: str, payload) -> str:
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    for stale in os.listdir(OUT):
        os.remove(os.path.join(OUT, stale))
    v = validator("example-validator-#1")
    chain_a, chain_b = "chain-a-1", "chain-b-1"

    h1, h2 = hashlib.sha256(b"block X").hexdigest(), hashlib.sha256(b"block Y").hexdigest()

    # Setup payloads (each directly POST-able).
    write("00_chain.json", {"chain_id": chain_a})
    write("00_validator.json", {"chain_id": chain_a, "public_key": v["public_key"],
                                "moniker": "example-validator-1"})
    write("00_snapshot.json", {
        "chain_id": chain_a, "validator_address": v["validator_address"],
        "epoch": 0, "voting_power": 1_000_000,
    })
    # Combined overview for humans.
    write("00_setup_overview.json", {
        "chain": {"chain_id": chain_a},
        "validator": {"chain_id": chain_a, "public_key": v["public_key"],
                      "moniker": "example-validator-1"},
        "snapshot_epoch0": {
            "chain_id": chain_a, "validator_address": v["validator_address"],
            "epoch": 0, "voting_power": 1_000_000,
        },
    })

    # Double sign: vote A then vote B (forward order).
    va = make_vote(v, chain_id=chain_a, height=42, round=3,
                   vote_type="prevote", block_hash=h1)
    vb = make_vote(v, chain_id=chain_a, height=42, round=3,
                   vote_type="prevote", block_hash=h2)
    write("10_vote_blockX.json", va)
    write("11_vote_blockY.json", vb)

    # Reverse order (same evidence id as forward order).
    write("12_reverse_blockY.json", vb)
    write("13_reverse_blockX.json", va)

    # Identical repeat — not a double sign.
    write("14_identical_repeat.json", va)

    # Invalid: zeroed signature.
    bad = dict(va)
    bad["signature"] = "00" * 64
    write("20_bad_signature.json", bad)

    # Invalid: replay exact signed bytes on another chain (cross-chain mix).
    cross = dict(va)
    cross["chain_id"] = chain_b
    write("21_cross_chain_replay.json", cross)

    # Invalid: tampered block hash but old signature.
    tampered = dict(va)
    tampered["block_hash"] = h2
    write("22_tampered_blockhash.json", tampered)

    summary = {
        "validator_address": v["validator_address"],
        "public_key": v["public_key"],
        "chain_id": chain_a,
        "evidence_round": 3,
        "note": "votes 10+11 (and 12+13) canonicalize to the SAME evidence id",
    }
    write("README.json", summary)
    print(f"wrote examples to {OUT}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
