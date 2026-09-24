#!/usr/bin/env python3
"""Reference signing client for the grid-probability-fusion HTTP service.

Pure standard library. Implements exactly the canonical request + HMAC-SHA256
scheme documented in README.md, so it doubles as a protocol specification:

    SIGNED-REQUEST:v1
    method=<UPPER>
    target=<path?with-sorted-query-pairs>
    timestamp=<unix-seconds>
    nonce=<unique-string>
    body_sha256=<hex sha256 of the exact body bytes>

    X-PF-Signature: hex HMAC_SHA256(secret, canonical_string)

Usage:
    PF_SECRET=... ./pf_client.py create-map examples/create_map.json
    PF_SECRET=... ./pf_client.py apply-scan <map_id> examples/scan_wall.json
    PF_SECRET=... ./pf_client.py export <map_id> [--seq N] [--out f.json]
    PF_SECRET=... ./pf_client.py verify <map_id>
    PF_SECRET=... ./pf_client.py demo examples/   # end-to-end acceptance run

The secret can also be passed via --secret or read from --secret-file.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request


def canonical_target(target: str) -> str:
    if "?" not in target:
        return target
    path, query = target.split("?", 1)
    pairs = sorted(p for p in query.split("&") if p)
    return path + ("?" + "&".join(pairs) if pairs else "")


def canonical_request(method, target, timestamp, nonce, body_sha256_hex):
    return (
        "SIGNED-REQUEST:v1\n"
        f"method={method.upper()}\n"
        f"target={canonical_target(target)}\n"
        f"timestamp={timestamp}\n"
        f"nonce={nonce}\n"
        f"body_sha256={body_sha256_hex}\n"
    )


def sign_headers(method, target, body_bytes, secret, timestamp=None,
                 nonce=None):
    timestamp = int(time.time()) if timestamp is None else timestamp
    nonce = secrets.token_urlsafe(12) if nonce is None else nonce
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    canonical = canonical_request(method, target, timestamp, nonce, body_hash)
    sig = hmac.new(secret.encode(), canonical.encode(),
                   hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-PF-Timestamp": str(timestamp),
        "X-PF-Nonce": nonce,
        "X-PF-Signature": sig,
    }


def request(base, method, target, secret, body=None):
    body_bytes = b"" if body is None else body.encode()
    headers = sign_headers(method, target, body_bytes, secret)
    req = urllib.request.Request(
        base.rstrip("/") + target, data=(body_bytes or None),
        headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def cmd_create_map(args, secret):
    body = open(args.path).read()
    # Validate it is JSON before sending.
    json.loads(body)
    status, text = request(args.base, "POST", "/v1/maps", secret, body)
    print(text)
    if status != 201:
        sys.exit(f"create-map failed: HTTP {status}")
    return json.loads(text)["id"]


def cmd_apply_scan(args, secret):
    body = open(args.path).read()
    json.loads(body)
    target = f"/v1/maps/{args.map_id}/scans"
    status, text = request(args.base, "POST", target, secret, body)
    print(text)
    if status not in (200, 201):
        sys.exit(f"apply-scan failed: HTTP {status}")


def cmd_export(args, secret):
    target = f"/v1/maps/{args.map_id}/grid"
    if args.seq is not None:
        target += f"?seq={args.seq}"
    status, text = request(args.base, "GET", target, secret)
    if status != 200:
        sys.exit(f"export failed: HTTP {status}\n{text}")
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)


def cmd_verify(args, secret):
    status, text = request(
        args.base, "GET", f"/v1/maps/{args.map_id}/verify", secret)
    print(text)
    if status != 200 or not json.loads(text).get("ok"):
        sys.exit(f"verify failed: HTTP {status}")


def cmd_demo(args, secret):
    """End-to-end demo driven by the files in a directory."""
    d = args.path
    status, text = request(args.base, "POST", "/v1/maps", secret,
                           open(os.path.join(d, "create_map.json")).read())
    assert status == 201, (status, text)
    map_id = json.loads(text)["id"]
    print(f"map_id={map_id}")

    for name in ("scan_wall.json", "scan_no_return.json",
                 "scan_raw_beams.json"):
        p = os.path.join(d, name)
        if not os.path.exists(p):
            continue
        status, text = request(
            args.base, "POST", f"/v1/maps/{map_id}/scans", secret,
            open(p).read())
        j = json.loads(text)
        print(f"{name}: HTTP {status} seq={j.get('latest_seq')} "
              f"state={j.get('latest_version_digest', '')[:16]}...")
        assert status == 200, (status, text)

    status, text = request(
        args.base, "GET", f"/v1/maps/{map_id}/verify", secret)
    print("verify:", text)
    assert status == 200 and json.loads(text)["ok"]

    status, text = request(
        args.base, "GET", f"/v1/maps/{map_id}/grid", secret)
    grid = json.loads(text)
    observed = sum(
        1 for row in grid["grid"]["cells"] for c in row
        if c["state"] == "observed")
    unknown = grid["config"]["width"] * grid["config"]["height"] - observed
    print(f"observed={observed} unknown={unknown} "
          f"state_digest={grid['state_digest'][:16]}...")

    # Tamper check: sign one body, send a different one -> must be 401.
    body = open(os.path.join(d, "scan_wall.json")).read()
    good = sign_headers("POST", f"/v1/maps/{map_id}/scans",
                        body.encode(), secret)
    req = urllib.request.Request(
        args.base.rstrip("/") + f"/v1/maps/{map_id}/scans",
        data=(body + " ").encode(), headers=good, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
        sys.exit("security check FAILED: tampered body accepted")
    except urllib.error.HTTPError as e:
        assert e.code == 401, e.code
        print("tamper check: 401 (expected)")
    print("DEMO OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--secret", default=None)
    ap.add_argument("--secret-file", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-map"); p.add_argument("path")
    p.set_defaults(func=cmd_create_map)

    p = sub.add_parser("apply-scan")
    p.add_argument("map_id"); p.add_argument("path")
    p.set_defaults(func=cmd_apply_scan)

    p = sub.add_parser("export")
    p.add_argument("map_id"); p.add_argument("--seq", type=int, default=None)
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("verify"); p.add_argument("map_id")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("demo"); p.add_argument("path")
    p.set_defaults(func=cmd_demo)

    args = ap.parse_args()
    if args.secret:
        secret = args.secret
    elif args.secret_file:
        secret = open(args.secret_file).read().strip()
    elif os.environ.get("PF_SECRET"):
        secret = os.environ["PF_SECRET"]
    else:
        sys.exit("provide --secret, --secret-file, or PF_SECRET")
    args.func(args, secret)


if __name__ == "__main__":
    main()
