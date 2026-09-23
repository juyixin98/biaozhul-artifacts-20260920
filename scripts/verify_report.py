"""校验报告哈希链与服务器 Ed25519 签名（真实验签）。

用法：
  python scripts/verify_report.py            # 从本地服务 GET /reports/latest
  python scripts/verify_report.py <file.json>
服务地址可用 BASE_URL 覆盖，默认 http://127.0.0.1:8000。
"""
from __future__ import annotations

import json
import os
import sys

import httpx
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import crypto  # noqa: E402


def fetch_report() -> dict:
    if len(sys.argv) == 2:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            return json.load(f)
    base = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
    r = httpx.get(f"{base}/reports/latest", timeout=10)
    r.raise_for_status()
    return r.json()


def main() -> int:
    report = fetch_report()
    body = report["body"]
    digest = crypto.digest_hex(body)
    assert digest == report["digest"], "报告摘要不一致"

    pub_path = os.path.join(os.path.dirname(__file__), "..", "keys", "server_public.hex")
    env = os.environ.get("SERVER_PUBLIC_KEY_HEX", "")
    vk = VerifyKey(
        bytes.fromhex(env) if env else bytes.fromhex(
            open(pub_path, "r", encoding="ascii").read().strip()
        )
    )
    try:
        vk.verify(crypto.report_digest(body), bytes.fromhex(report["signature"]))
    except BadSignatureError:
        print("签名验证失败")
        return 1

    print(f"version={body['version']} digest={digest} 签名有效")
    print(f"prev_hash={body['prev_hash']} late_recompute={body['late_recompute']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
