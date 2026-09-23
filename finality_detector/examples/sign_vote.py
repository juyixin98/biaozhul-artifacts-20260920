#!/usr/bin/env python3
"""Sign a vote with a demo private key and print the JSON body for POST /votes.

Usage:
    python examples/sign_vote.py <epoch> <height> <value> <validator_id> <private_key_b64>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import crypto  # noqa: E402
from app.config import settings  # noqa: E402


def main() -> None:
    if len(sys.argv) != 6:
        sys.exit(__doc__)
    epoch, height, value, validator_id, priv_b64 = (
        int(sys.argv[1]),
        int(sys.argv[2]),
        sys.argv[3],
        sys.argv[4],
        sys.argv[5],
    )
    sk = crypto.decode_private_key(priv_b64)
    signature = crypto.sign_vote(sk, settings.chain_id, epoch, height, value)
    print(
        json.dumps(
            {
                "epoch_id": epoch,
                "height": height,
                "validator_id": validator_id,
                "value": value,
                "signature": signature,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
