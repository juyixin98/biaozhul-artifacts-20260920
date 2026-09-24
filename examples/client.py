"""Command-line client example: analyze a .tl file and verify the signature.

Usage:
    .venv/bin/python examples/client.py tests/fixtures/cross_function.tl
"""
from __future__ import annotations

import base64
import json
import sys
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

BASE = "http://127.0.0.1:8000"


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def get(path: str) -> bytes:
    with urllib.request.urlopen(BASE + path) as resp:
        return resp.read()


def main(path: str) -> int:
    code = open(path, encoding="utf-8").read()
    body = post("/analyze", {"code": code})
    report, signature_b64 = body["report"], body["signature"]

    pem = get("/public-key")
    key = serialization.load_pem_public_key(pem)
    assert isinstance(key, Ed25519PublicKey)
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    try:
        key.verify(base64.b64decode(signature_b64), canonical)
        verified = True
    except Exception:
        verified = False

    print(f"file:       {path}")
    print(f"vulnerable: {report['vulnerable']}")
    print(f"findings:   {report['finding_count']}")
    print(f"signature:  {'VALID' if verified else 'INVALID'} (Ed25519)")
    for f in report["findings"]:
        chain = " -> ".join(f["call_chain"]) or "(top-level sink)"
        print(f"  - source {f['source']['func']}@{f['source']['loc']} "
              f"-> sink {f['sink']['func']}@{f['sink']['loc']}")
        print(f"    call chain: {chain}  ({f['path_length']} steps)")
        for step in f["path"]:
            print(f"      [{step['loc']:>6}] {step['kind']:<7} "
                  f"{step['func']:<14} {step['detail']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
