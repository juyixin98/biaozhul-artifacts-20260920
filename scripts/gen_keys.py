#!/usr/bin/env python3
"""Generate fresh random keys for a registry (prints a ready-to-paste block)."""

import base64
import json
import os
import sys


def new_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()


def main() -> None:
    if len(sys.argv) < 2:
        for label in ("epoch_key", "tester_key_1", "tester_key_2"):
            print(f"{label} = {new_key()}")
        print("\nUsage: gen_keys.py <output_registry.json>  (writes a fresh template)")
        return

    out = sys.argv[1]
    registry = {
        "admin_key": base64.urlsafe_b64encode(os.urandom(24)).rstrip(b"=").decode(),
        "default_ttl_seconds": 5.0,
        "epoch_key": new_key(),
        "testers": {
            "tester_alpha": {"key": new_key(), "robots": ["alpha", "bravo"]},
        },
        "robots": {
            "alpha": {"namespace": "/p48/alpha"},
            "bravo": {"namespace": "/p48/bravo"},
        },
    }
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(registry, fh, indent=2)
    print(f"wrote fresh registry to {out}")


if __name__ == "__main__":
    main()
