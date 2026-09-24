#!/usr/bin/env python3
"""Freeze the parameter manifest: create a dev HMAC key (if missing) and
write params.sha256 + params.sig next to params.json.

Real key management: set BTE_PARAM_KEY (hex, 32+ bytes) in production and
keep it out of the repo. The committed param_key.dev.hex is DEVELOPMENT
ONLY so the service runs out of the box.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.params import sign_manifest  # noqa: E402
from app.util import sha256_hex  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--param-dir", default="config")
    ap.add_argument("--force-key", action="store_true", help="regenerate the dev key even if present")
    args = ap.parse_args()

    param_dir = Path(args.param_dir)
    manifest = json.loads((param_dir / "params.json").read_text(encoding="utf-8"))

    key_file = param_dir / "param_key.dev.hex"
    if args.force_key or not key_file.exists():
        key_file.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
        print(f"generated NEW development key: {key_file} (DEV ONLY — do not use in production)")
    key = bytes.fromhex(key_file.read_text(encoding="utf-8").strip())

    digest = sha256_hex(manifest)
    sig = sign_manifest(manifest, key)
    (param_dir / "params.sha256").write_text(digest + "\n", encoding="utf-8")
    (param_dir / "params.sig").write_text(sig + "\n", encoding="utf-8")
    print(f"version : {manifest['version']}")
    print(f"sha256  : {digest}")
    print(f"hmac    : {sig}")
    print("frozen. The service verifies this HMAC at startup.")


if __name__ == "__main__":
    main()
