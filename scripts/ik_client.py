"""
Reference client for the IK service. Signs requests with a real
HMAC-SHA256 over a canonical string (method, path, timestamp, body hash)
and performs constant-time-equivalent verification on the client side
of the response status.

Usage:
    .venv/bin/python scripts/ik_client.py examples/01_known_fk_target.json
    IK_KEY_ID=my-id IK_API_SECRET=my-secret \\
        .venv/bin/python scripts/ik_client.py examples/04_out_of_reach.json \\
        --url http://127.0.0.1:8000/api/v1/ik/solve
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request


def sign(method: str, path: str, timestamp: str, body: bytes,
         secret: bytes) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = f"{method.upper()}\n{path}\n{timestamp}\n{body_hash}".encode()
    return hmac.new(secret, canonical, hashlib.sha256).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("payload", help="path to request JSON file")
    ap.add_argument("--url", default="http://127.0.0.1:8000/api/v1/ik/solve")
    args = ap.parse_args()

    key_id = os.environ.get("IK_KEY_ID", "demo-key-id")
    secret = os.environ.get("IK_API_SECRET", "demo-secret").encode()

    with open(args.payload, "rb") as f:
        body = f.read()
    # Re-serialise deterministically so the server sees exactly what we sign
    body = json.dumps(json.loads(body), separators=(",", ":")).encode()

    from urllib.parse import urlparse
    path = urlparse(args.url).path
    ts = str(int(time.time()))
    sig = sign("POST", path, ts, body, secret)

    req = urllib.request.Request(args.url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-Key": key_id,
        "X-Timestamp": ts,
        "X-Signature": sig,
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode(), file=sys.stderr)
        return 2

    print(json.dumps(data, indent=2))
    # Client-side policy: never report success unless verification passed
    if data.get("status") == "ok":
        v = data.get("verification", {})
        if not v.get("passed"):
            print("REFUSING unverified success", file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
