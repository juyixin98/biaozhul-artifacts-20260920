"""命令行客户端：向 /estimate 发起带 HMAC-SHA256 签名的请求（仅用标准库）。

用法：
  # 服务端未启用鉴权时：
  python scripts/signed_request.py examples/spikes.json --no-auth

  # 服务端设置了 IMU_BIAS_HMAC_SECRET 时：
  IMU_BIAS_HMAC_SECRET=... python scripts/signed_request.py examples/spikes.json

可选 --base-url http://127.0.0.1:8000
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="请求 JSON 文件路径")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--no-auth", action="store_true", help="不计算签名")
    args = ap.parse_args()

    with open(args.input, "rb") as fh:
        body = fh.read()
    # 重新规范化输出，保证服务端收到的字节与本地哈希一致（本脚本直接发送文件原字节，
    # 因此必须对“文件原字节”签名）
    url = args.base_url.rstrip("/") + "/estimate"
    headers = {"Content-Type": "application/json"}
    if not args.no_auth:
        secret = os.environ.get("IMU_BIAS_HMAC_SECRET", "").encode()
        if not secret:
            print("error: 需要设置 IMU_BIAS_HMAC_SECRET 或传 --no-auth", file=sys.stderr)
            return 2
        ts = f"{time.time():.3f}"
        body_hash = hashlib.sha256(body).hexdigest()
        msg = f"{ts}.POST./estimate.{body_hash}".encode()
        headers["X-Timestamp"] = ts
        headers["X-Signature"] = "sha256=" + hmac.new(secret, msg, hashlib.sha256).hexdigest()

    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            status = r.status
            resp_sig = r.headers.get("X-Response-Signature")
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
        resp_sig = None

    if not args.no_auth and resp_sig:
        expect = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
        verified = hmac.compare_digest(resp_sig, expect)
        print(f"# HTTP {status}; response signature verified: {verified}", file=sys.stderr)
    else:
        print(f"# HTTP {status}", file=sys.stderr)

    try:
        print(json.dumps(json.loads(raw), ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        print(raw.decode("utf-8", "replace"))
    return 0 if status == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
