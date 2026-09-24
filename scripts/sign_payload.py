#!/usr/bin/env python3
"""Sign a JSON calibration payload with an Ed25519 private key.

Usage:
    python scripts/sign_payload.py <private_key.pem> <payload.json> [out.json]

Canonicalises the payload (sorted keys, no whitespace, signature dropped),
prints the SHA-256 fingerprint and hex signature, and optionally writes a
signed bundle {"payload": ..., "signature": ..., "public_key": ...}.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crypto import (
    canonical_json,
    fingerprint,
    load_private_key,
    public_key_hex,
    sign,
)


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(__doc__)
        return 2
    key_path, payload_path = argv[1], Path(argv[2])
    priv = load_private_key(key_path)
    payload = json.loads(payload_path.read_text())
    canonical = canonical_json(payload)
    signature = sign(priv, payload)
    print(f"canonical_sha256: {fingerprint(payload)}")
    print(f"signature (hex) : {signature}")
    print(f"public_key (hex): {public_key_hex(priv.public_key())}")
    if len(argv) == 4:
        bundle = {
            "payload": payload,
            "signature": signature,
            "public_key": public_key_hex(priv.public_key()),
        }
        Path(argv[3]).write_text(json.dumps(bundle, indent=2, ensure_ascii=False))
        print(f"bundle written  : {argv[3]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
