#!/usr/bin/env python3
"""End-to-end walkthrough against a running server (stdlib only, no deps).

Start the service first, e.g.:

    FDD_DB_PATH=/tmp/fdd-demo.db FDD_RESET_ON_START=true \
        .venv/bin/uvicorn app.main:app --port 8000 --app-dir finality_detector

Then run:

    python examples/demo.py [base_url]

The script demonstrates, with real signatures and real HTTP calls:
  1. epoch creation with a weighted validator set
  2. finality once weight strictly exceeds 2/3 (integer comparison)
  3. a double vote -> conflict evidence stored, validator excluded
  4. a cross-epoch replayed signature being rejected
  5. a conflicting quorum certificate -> alarm + frozen epoch
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import crypto  # noqa: E402
from app.config import settings  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def show(title: str, status: int, payload: dict) -> None:
    print(f"\n=== {title} (HTTP {status}) ===")
    print(json.dumps(payload, indent=2)[:2000])


def main() -> None:
    chain_id = settings.chain_id

    # --- validators: weights 40 / 30 / 20 / 10, total 100, quorum needs 67 ---
    keys = {f"validator-{i}": crypto.generate_private_key() for i in range(1, 5)}
    weights = {"validator-1": 40, "validator-2": 30, "validator-3": 20, "validator-4": 10}
    validators = [
        {
            "validator_id": vid,
            "weight": weights[vid],
            "public_key": crypto.encode_public_key(sk.public_key()),
        }
        for vid, sk in keys.items()
    ]

    def vote(epoch: int, height: int, vid: str, value: str) -> dict:
        return {
            "epoch_id": epoch,
            "height": height,
            "validator_id": vid,
            "value": value,
            "signature": crypto.sign_vote(keys[vid], chain_id, epoch, height, value),
        }

    status, body = call("POST", "/epochs", {"epoch_id": 0, "height": 100, "validators": validators})
    show("create epoch 0 (total weight 100, required power 67)", status, body)

    status, body = call("POST", "/votes", vote(0, 100, "validator-1", "block-A"))
    show("vote v1 (weight 40) for block-A -> not final", status, body)

    status, body = call("POST", "/votes", vote(0, 100, "validator-2", "block-A"))
    show("vote v2 (40+30=70 > 2/3*100) -> block-A finalized", status, body)

    status, body = call("POST", "/votes", vote(0, 100, "validator-3", "block-B"))
    show("v3 double-votes block-B after block-A? (v3 only voted B here)", status, body)

    # validator-3 equivocates: first B, then A — second signature is the proof.
    status, body = call("POST", "/votes", vote(0, 100, "validator-3", "block-A"))
    show("v3 signs block-A too -> equivocation evidence + exclusion", status, body)

    status, body = call("GET", "/epochs/0/evidence")
    show("stored double-vote evidence for epoch 0", status, body)

    # --- cross-epoch replay: a signature made for epoch 0 must fail in epoch 1
    status, body = call("POST", "/epochs", {"epoch_id": 1, "height": 200, "validators": validators})
    show("create epoch 1", status, body)

    replayed = vote(0, 100, "validator-4", "block-A")
    replayed["epoch_id"] = 1
    replayed["height"] = 200
    status, body = call("POST", "/votes", replayed)
    show("replayed epoch-0 signature into epoch 1 -> rejected", status, body)

    # --- finality conflict: finalize block-C in epoch 1, then present a
    #     conflicting quorum certificate for block-D ---
    for vid in ("validator-1", "validator-2", "validator-3"):
        status, body = call("POST", "/votes", vote(1, 200, vid, "block-C"))
    show("epoch 1 finalizes block-C (weight 90)", status, body)

    cert = {
        "epoch_id": 1,
        "height": 200,
        "value": "block-D",
        "signatures": [
            {"validator_id": vid, "signature": crypto.sign_vote(keys[vid], chain_id, 1, 200, "block-D")}
            for vid in ("validator-1", "validator-2", "validator-4")
        ],
    }
    status, body = call("POST", "/certificates", cert)
    show("conflicting certificate for block-D (weight 80) -> ALARM + freeze", status, body)

    status, body = call("GET", "/alarms")
    show("open alarms", status, body)

    status, body = call("GET", "/epochs/1")
    show("epoch 1 status: still finalized block-C, now frozen", status, body)

    status, body = call("POST", "/epochs", {"epoch_id": 2, "height": 300, "validators": validators})
    show("attempt to advance to epoch 2 while epoch 1 frozen -> refused", status, body)

    print("\nDemo finished. Restart the server and GET /health or /rebuild to see")
    print("the same conclusions reconstructed from the SQLite database.")


if __name__ == "__main__":
    main()
