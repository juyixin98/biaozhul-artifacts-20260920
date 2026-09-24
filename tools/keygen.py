#!/usr/bin/env python3
"""Generate the server Ed25519 signing key.

Outputs:
  <dir>/audit_signing_key.pem         (private, mode 0600 - server only)
  <dir>/audit_signing_key.public.pem  (public  - the trust anchor to hand to
                                       verifiers via a trusted channel)

Usage:  python -m tools.keygen --key-dir keys
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app import keys as keymod


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--key-dir", default="keys", help="output directory")
    parser.add_argument(
        "--force", action="store_true", help="overwrite an existing key pair"
    )
    args = parser.parse_args(argv)

    out = Path(args.key_dir)
    priv = out / "audit_signing_key.pem"
    pub = out / "audit_signing_key.public.pem"
    if priv.exists() and not args.force:
        print(f"refusing to overwrite existing key {priv} (use --force)", file=sys.stderr)
        return 2

    key = keymod.generate_private_key()
    keymod.save_private_key(priv, key)
    pub.write_bytes(keymod.export_public_key_pem(key))

    print("wrote", priv, "(private - server only, mode 0600)")
    print("wrote", pub, "(public trust anchor - distribute out-of-band)")
    print("trust anchor hex:", keymod.public_key_hex(key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
