"""Sign and send a request against the local verification server.

Reads the body from stdin (or --body), builds the canonical request, signs it
with the selected local key, prints the canonical string for inspection, sends
the request, and prints a ready-to-paste curl command (without the secret).

Usage:
    echo '{"op":"ping"}' | python3 -m scripts.sign_request \\
        --url http://127.0.0.1:8080/v1/verify --method POST --kid test-key-1
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from replay_protection.keys import KeyRegistry
from replay_protection.signing import SIGNING_ALGORITHM, canonical_request, sign_request


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--method", default="POST")
    ap.add_argument("--kid", default="test-key-1")
    ap.add_argument("--keys", default="data/keys.json")
    ap.add_argument("--body", help="literal body; default reads stdin")
    ap.add_argument("--timestamp", type=int, default=None, help="override ts (testing)")
    ap.add_argument("--nonce", default=None, help="override nonce (testing)")
    ap.add_argument("--send", action="store_true", help="actually perform the request")
    args = ap.parse_args()

    body = args.body.encode("utf-8") if args.body is not None else sys.stdin.buffer.read()
    parts = urlsplit(args.url)
    target = parts.path + (f"?{parts.query}" if parts.query else "")

    registry = KeyRegistry.load(args.keys)
    secret = registry.get(args.kid)
    if secret is None:
        print(f"unknown kid {args.kid!r}; have: {registry.ids()}", file=sys.stderr)
        return 2

    ts = args.timestamp if args.timestamp is not None else int(time.time())
    import secrets as _secrets

    nonce = args.nonce or _secrets.token_urlsafe(24).replace("-", "_").replace("/", "_")
    # token_urlsafe(24) yields ~32 chars from the right alphabet already, but
    # guarantee length/charset constraints explicitly.
    nonce = nonce[:64].ljust(16, "0")

    canonical = canonical_request(
        args.method.upper(), target, body, key_id=args.kid, timestamp=ts, nonce=nonce
    )
    signature = sign_request(secret, canonical)
    auth = f"{SIGNING_ALGORITHM} Credential={args.kid}, Signature={signature}"

    print("----- canonical request (signed string) -----")
    print(canonical)
    print("----------------------------------------------")
    print("X-Key-Id:     ", args.kid)
    print("X-Timestamp:  ", ts)
    print("X-Nonce:      ", nonce)
    print("Authorization:", auth)
    print()
    print("# replay with curl:")
    print(
        f"curl -sS -X {args.method.upper()} '{args.url}' "
        f"-H 'X-Key-Id: {args.kid}' -H 'X-Timestamp: {ts}' "
        f"-H 'X-Nonce: {nonce}' -H 'Authorization: {auth}' "
        f"--data-binary @- <<'EOF'\n{body.decode('utf-8', 'replace')}\nEOF"
    )

    if args.send:
        req = urllib.request.Request(
            args.url,
            data=body,
            method=args.method.upper(),
            headers={
                "X-Key-Id": args.kid,
                "X-Timestamp": str(ts),
                "X-Nonce": nonce,
                "Authorization": auth,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                print("\nserver response:", resp.status, resp.read().decode())
        except urllib.error.HTTPError as e:
            print("\nserver response:", e.code, e.read().decode())
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
