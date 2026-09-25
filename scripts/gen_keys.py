"""Generate local test keys and write them to a JSON file (mode 0600)."""

from __future__ import annotations

import argparse
import os

from replay_protection.keys import generate_key, write_key_file


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate local HMAC test keys")
    parser.add_argument("--out", default=os.path.join("data", "keys.json"))
    parser.add_argument(
        "--fixed-id",
        default="test-key-1",
        help="include one well-known kid for the examples/tests (default test-key-1)",
    )
    args = parser.parse_args()

    fixed = (args.fixed_id, None)
    kid, secret_hex = generate_key()
    # Deterministic kid for documentation; secret itself is always random.
    entries = [(args.fixed_id, secret_hex)]
    kid2, secret_hex2 = generate_key()
    entries.append((kid2, secret_hex2))

    path = write_key_file(args.out, entries)
    print(f"wrote {len(entries)} keys to {path} (mode 0600)")
    print(f"  {args.fixed_id}")
    print(f"  {kid2}")
    print("secrets are random per run; never commit or reuse outside local testing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
