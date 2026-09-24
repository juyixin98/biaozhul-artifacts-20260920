#!/usr/bin/env python3
"""用 Ed25519 私钥为策略文件签发签名包。

用法:
    python scripts/sign_policy.py examples/policies.json \\
        --private-key keys/dev_private.pem \\
        --kid 2026-09-dev \\
        --out examples/signed_bundle.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.crypto import load_private_key_pem, sign_policies


def main() -> int:
    parser = argparse.ArgumentParser(description="为策略集签发 Ed25519 签名包")
    parser.add_argument("policies", help="策略集 JSON 文件（policy 数组）")
    parser.add_argument("--private-key", default="keys/dev_private.pem")
    parser.add_argument("--kid", default="2026-09-dev")
    parser.add_argument("--out", default="examples/signed_bundle.json")
    args = parser.parse_args()

    with open(args.policies, "r", encoding="utf-8") as fh:
        policies = json.load(fh)
    with open(args.private_key, "rb") as fh:
        priv = load_private_key_pem(fh.read())

    bundle = sign_policies(policies, priv, kid=args.kid)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"已签名 -> {args.out}（kid={args.kid}, alg=Ed25519, {len(bundle['signature'])} b64 chars）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
