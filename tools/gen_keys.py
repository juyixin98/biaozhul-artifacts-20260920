"""Generate an Ed25519 offline signing keypair.

Usage::

    python -m tools.gen_keys --out examples/keys --name demo

Writes ``<name>.pem`` (private key, keep secret) and
``<name>.pub.pem`` (public trust anchor to provision into POLICY_TRUST_DIR).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.signing import (
    generate_keypair,
    key_id,
    private_key_pem,
    public_key_pem,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="examples/keys",
                        help="output directory")
    parser.add_argument("--name", default="demo", help="key file basename")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    priv_path = out_dir / f"{args.name}.pem"
    pub_path = out_dir / f"{args.name}.pub.pem"
    if priv_path.exists() or pub_path.exists():
        raise SystemExit(f"refusing to overwrite existing key in {out_dir}")

    private, public = generate_keypair()
    priv_path.write_bytes(private_key_pem(private))
    # chmod 600 for the private key where the platform supports it.
    try:
        priv_path.chmod(0o600)
    except OSError:
        pass
    pub_path.write_bytes(public_key_pem(public))
    print(f"private key : {priv_path}")
    print(f"public key  : {pub_path}")
    print(f"kid (fingerprint): {key_id(public)}")


if __name__ == "__main__":
    main()
