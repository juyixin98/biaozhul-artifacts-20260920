#!/usr/bin/env python3
"""Generate a demo validator key set (real Ed25519 keys).

Usage:
    python examples/gen_keys.py [N] [--weights w1,w2,...]

Prints JSON to stdout:
    {
      "chain_id": ...,
      "validators": [{"validator_id", "weight", "public_key"}...],
      "private_keys": {"validator_id": "<base64 private key>"}
    }

The ``validators`` array can be pasted straight into POST /epochs.
Keep the private keys local — they are only needed to sign votes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import crypto  # noqa: E402
from app.config import settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("n", type=int, nargs="?", default=4, help="number of validators")
    parser.add_argument("--weights", type=str, default=None, help="comma separated weights")
    args = parser.parse_args()

    if args.weights:
        weights = [int(w) for w in args.weights.split(",")]
        if len(weights) != args.n:
            parser.error("--weights must have exactly N entries")
    else:
        weights = [1] * args.n

    validators = []
    private_keys = {}
    for i in range(args.n):
        sk = crypto.generate_private_key()
        vid = f"validator-{i + 1}"
        validators.append(
            {
                "validator_id": vid,
                "weight": weights[i],
                "public_key": crypto.encode_public_key(sk.public_key()),
            }
        )
        private_keys[vid] = crypto.encode_private_key(sk)

    print(
        json.dumps(
            {"chain_id": settings.chain_id, "validators": validators, "private_keys": private_keys},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
