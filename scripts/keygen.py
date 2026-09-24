#!/usr/bin/env python3
"""Generate a real Ed25519 keypair for signing calibration bundles.

Usage:
    python scripts/keygen.py keys/calibration_signing_key

Writes <name>.pem (PKCS8 private key, mode 0600) and
<name>.pub.pem (SubjectPublicKeyInfo) plus a .hex public key.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crypto import (
    private_key_pem,
    public_key_hex,
    public_key_pem,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    base = Path(argv[1])
    base.parent.mkdir(parents=True, exist_ok=True)
    priv = Ed25519PrivateKey.generate()
    priv_path = Path(str(base) + ".pem")
    pub_path = Path(str(base) + ".pub.pem")
    hex_path = Path(str(base) + ".pub.hex")
    priv_path.write_bytes(private_key_pem(priv))
    priv_path.chmod(0o600)
    pub_path.write_bytes(public_key_pem(priv.public_key()))
    hex_path.write_text(public_key_hex(priv.public_key()) + "\n")
    print(f"private key : {priv_path}")
    print(f"public key  : {pub_path}")
    print(f"public hex  : {hex_path}")
    print("algorithm   : Ed25519 (RFC 8032)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
