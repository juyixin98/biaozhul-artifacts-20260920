#!/usr/bin/env python3
"""Print a ready-to-paste signed curl command for a robot command.

Real HMAC-SHA256 signature over (method, path, tester, nonce, timestamp,
sha256(body)).  This is the same computation the client library performs;
use it to drive the gateway with plain curl.

  python3 examples/sign_curl.py alpha tester_alpha examples/command_valid.json
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from p48_gateway.crypto import hmac_sign, request_signing_payload  # noqa: E402
from p48_gateway.registry import load_registry  # noqa: E402

PORT = os.environ.get("P48_PORT", "8088")
REG = os.environ.get("P48_REGISTRY", "config/registry.example.json")


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    robot, tester, body_path = sys.argv[1:4]
    with open(body_path, "r", encoding="utf-8") as fh:
        body_obj = json.load(fh)
    registry = load_registry(REG)
    if tester not in registry.testers:
        print(f"unknown tester {tester!r}")
        return 2
    key = registry.testers[tester].key

    path = f"/robots/{robot}/commands"
    ts = time.time()
    nonce = uuid.uuid4().hex
    payload = request_signing_payload("POST", path, tester, nonce, ts, body_obj)
    sig = hmac_sign(payload, key)
    body_json = json.dumps(body_obj, separators=(",", ":"))

    url = f"http://127.0.0.1:{PORT}{path}"
    cmd = [
        "curl", "-sS", "-X", "POST", url,
        "-H", "Content-Type: application/json",
        "-H", f"X-Tester: {tester}",
        "-H", f"X-Timestamp: {ts:.6f}",
        "-H", f"X-Nonce: {nonce}",
        "-H", f"X-Signature: {sig}",
        "--data", body_json,
    ]
    print(" ".join(shlex.quote(part) for part in cmd))
    print("\n# signed canonical payload (what the HMAC covers):", payload.decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
