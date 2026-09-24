#!/usr/bin/env python3
"""Sign the frozen parameter file with Ed25519.

This script is the parameter-release tool. The keypair in keys/ is a
DEVELOPMENT-ONLY test key (committed so the service runs out of the box);
production must use a separately held key and replace
keys/params_signing_public.pem and params/params_v1.sig.

Usage:
    python tools/sign_params.py                # verify only
    python tools/sign_params.py --generate-key # create dev keypair + sign
    python tools/sign_params.py --sign         # sign with existing private key
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
    load_pem_public_key,
    NoEncryption,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PARAMS_PATH = REPO_ROOT / "params" / "params_v1.json"
SIG_PATH = REPO_ROOT / "params" / "params_v1.sig"
KEYS_DIR = REPO_ROOT / "keys"
PRIV_PATH = KEYS_DIR / "params_signing_private.pem"
PUB_PATH = KEYS_DIR / "params_signing_public.pem"

sys.path.insert(0, str(REPO_ROOT))
from app.params import canonical_bytes, sign_params, verify_params  # noqa: E402


def generate_keypair() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    PRIV_PATH.write_bytes(
        key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    PUB_PATH.write_bytes(
        key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    PRIV_PATH.chmod(0o600)
    print(f"wrote {PRIV_PATH} and {PUB_PATH} (DEVELOPMENT-ONLY key)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate-key", action="store_true")
    parser.add_argument("--sign", action="store_true")
    args = parser.parse_args()

    if args.generate_key:
        generate_keypair()

    params = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))

    if args.sign:
        if not PRIV_PATH.exists():
            print(f"private key not found: {PRIV_PATH} (use --generate-key)", file=sys.stderr)
            return 2
        priv = load_pem_private_key(PRIV_PATH.read_bytes(), password=None)
        signature = sign_params(params, priv)
        SIG_PATH.write_text(signature.hex() + "\n", encoding="utf-8")
        print(f"signed {PARAMS_PATH} -> {SIG_PATH}")

    if not PUB_PATH.exists() or not SIG_PATH.exists():
        print("public key or signature missing", file=sys.stderr)
        return 2
    pub = load_pem_public_key(PUB_PATH.read_bytes())
    sig = bytes.fromhex(SIG_PATH.read_text(encoding="utf-8").strip())
    try:
        verify_params(params, sig, pub)
    except InvalidSignature:
        print("SIGNATURE INVALID", file=sys.stderr)
        return 1
    print(f"signature VALID for params_version={params['params_version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
