#!/usr/bin/env python3
"""Send a local /verify request using only the standard library.

Usage:
    python scripts/verify_request.py fixtures/good/request.json
    python scripts/verify_request.py fixtures/expired/request.json --port 8080

The script talks to 127.0.0.1 only. It performs no DNS lookups beyond the
loopback address and no outbound network calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib import request as urlrequest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("request_file", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8080/verify")
    args = parser.parse_args()

    body = args.request_file.read_bytes()
    # Validate JSON locally before sending.
    json.loads(body)
    req = urlrequest.Request(
        args.url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urlrequest.urlopen(req, timeout=10) as resp:  # noqa: S310 (loopback)
            payload = resp.read().decode("utf-8")
            status = resp.status
    except Exception as exc:  # noqa: BLE001
        print(f"request failed: {exc}", file=sys.stderr)
        return 2
    parsed = json.loads(payload)
    print(json.dumps(parsed, indent=2, sort_keys=True))
    print(f"\nHTTP {status} valid={parsed.get('valid')}", file=sys.stderr)
    return 0 if parsed.get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
