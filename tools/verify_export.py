#!/usr/bin/env python3
"""Standalone verifier - run on a machine that holds the trust anchor.

Examples:

  # Verify a full export file against the public anchor PEM:
  python -m tools.verify_export --anchor keys/audit_signing_key.public.pem \\
      --bundle export.json

  # Same, but the public key is given as raw hex:
  python -m tools.verify_export --anchor-hex <64-byte hex> --bundle export.json

  # Verify while remembering the latest checkpoint seen (bounded-trust
  # model): the first run caches the anchor checkpoint, later runs detect
  # truncation even if the server's exported checkpoint list was rewritten:
  python -m tools.verify_export --anchor key.public.pem --bundle export.json \\
      --remember anchor-cache.json

  # Verify an interior slice (checkpoints past the slice are NOT truncation):
  python -m tools.verify_export --anchor key.public.pem --bundle slice.json \\
      --bounded-range

Exit code: 0 VALID, 3 UNDETERMINED, 4 TRUNCATED, 5 TAMPERED, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app import keys as keymod
from app import verifier as vermod
from app.chain import checkpoint_message

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNDETERMINED = 3
EXIT_TRUNCATED = 4
EXIT_TAMPERED = 5


def _load_anchor_hex(args: argparse.Namespace) -> str:
    if args.anchor_hex:
        return args.anchor_hex.strip()
    if args.anchor:
        pem = Path(args.anchor).read_bytes()
        pub = keymod.import_public_key_pem(pem)
        return keymod.public_key_hex(pub)
    print("error: provide --anchor PEM or --anchor-hex", file=sys.stderr)
    raise SystemExit(EXIT_USAGE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bundle", required=True, help="export bundle JSON file")
    anchor = parser.add_mutually_exclusive_group(required=True)
    anchor.add_argument("--anchor", help="public key PEM file (trust anchor)")
    anchor.add_argument("--anchor-hex", help="public key raw hex (trust anchor)")
    parser.add_argument(
        "--remember",
        help="cache file for the latest trusted checkpoint (external anchor)",
    )
    parser.add_argument(
        "--bounded-range",
        action="store_true",
        help="bundle is an interior slice; do not treat coverage past end as truncation",
    )
    args = parser.parse_args(argv)

    anchor_hex = _load_anchor_hex(args)
    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))

    external = None
    if args.remember:
        cache_path = Path(args.remember)
        if cache_path.exists():
            external = json.loads(cache_path.read_text(encoding="utf-8"))

    result = vermod.verify_bundle(
        bundle,
        trust_anchor_hex=anchor_hex,
        external_anchor=external,
        expect_tail=not args.bounded_range,
    )

    if args.remember:
        # Cache the newest validly-signed checkpoint the verifier itself saw,
        # independently of what future servers choose to export.
        cps = [c for c in bundle.get("checkpoints", [])]
        if external is not None:
            cps.append(external)
        signed = []
        pub = keymod.load_public_key_hex(anchor_hex)
        for cp in cps:
            try:
                if keymod.verify_signature(
                    pub,
                    bytes.fromhex(cp["signature"]),
                    checkpoint_message(cp),
                ):
                    signed.append(cp)
            except (KeyError, ValueError, TypeError):
                pass
        if signed:
            newest = max(signed, key=lambda c: c["seq"])
            cache_path = Path(args.remember)
            if not cache_path.exists() or json.loads(cache_path.read_text())["seq"] < newest["seq"]:
                cache_path.write_text(
                    json.dumps(newest, indent=2, sort_keys=True), encoding="utf-8"
                )

    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    return {
        vermod.VALID: EXIT_OK,
        vermod.UNDETERMINED: EXIT_UNDETERMINED,
        vermod.TRUNCATED: EXIT_TRUNCATED,
        vermod.TAMPERED: EXIT_TAMPERED,
    }[result.status]


if __name__ == "__main__":
    raise SystemExit(main())
