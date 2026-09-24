#!/usr/bin/env python3
"""对 /api/v1/ik 请求做 HMAC-SHA256 签名并发送（真实密码学操作）。

用法：
  export IK_API_KEY='your-secret'
  python scripts/sign_request.py examples/01_reachable_target.json
  python scripts/sign_request.py examples/03_unreachable.json --base http://127.0.0.1:8000

若服务以 IK_REQUIRE_AUTH=false 启动，加 --no-auth 可跳过签名。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import uuid

import httpx


def sign(key: str, key_id: str, timestamp: str, nonce: str, raw: bytes) -> str:
    body_sha = hashlib.sha256(raw).hexdigest()
    msg = f"{key_id}\n{timestamp}\n{nonce}\n{body_sha}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("payload", help="请求 JSON 文件路径")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--no-auth", action="store_true", help="服务端关闭了鉴权")
    ap.add_argument("--key-id", default="default")
    args = ap.parse_args()

    raw = open(args.payload, "rb").read()
    # 先解析确认是合法 JSON（发送的字节与签名绑定，不重新序列化）
    json.loads(raw)

    headers = {"Content-Type": "application/json"}
    if not args.no_auth:
        key = os.environ.get("IK_API_KEY")
        if not key:
            print("错误：未设置 IK_API_KEY", file=sys.stderr)
            return 2
        ts = str(int(time.time()))
        nonce = uuid.uuid4().hex
        headers.update(
            {
                "X-IK-Key-Id": args.key_id,
                "X-IK-Timestamp": ts,
                "X-IK-Nonce": nonce,
                "X-IK-Signature": sign(key, args.key_id, ts, nonce, raw),
            }
        )

    # trust_env=False：忽略环境里的 HTTP(S)_PROXY/ALL_PROXY，直连目标
    # （本脚本用于本地/内网验收；如需经代理访问远端，请显式配置）
    with httpx.Client(trust_env=False) as client:
        r = client.post(f"{args.base}/api/v1/ik", content=raw, headers=headers, timeout=30)
    print(f"HTTP {r.status_code}")
    print(json.dumps(r.json(), ensure_ascii=False, indent=2))
    return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
