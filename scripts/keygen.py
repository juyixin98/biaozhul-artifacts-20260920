#!/usr/bin/env python3
"""生成本地测试密钥（OS CSPRNG）并写入 JSON keystore。

用法：
    python3 scripts/keygen.py --keystore dev-keys.json --key-id demo-key-1
    python3 scripts/keygen.py --keystore dev-keys.json --key-id demo-key-2 --overwrite

注意：仅供本地开发与自动化测试使用，密钥文件会被强制写成 0600 权限。
"""

from __future__ import annotations

import argparse
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from anti_replay.keys import KeystoreError, create_or_update_key  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成本地 HMAC 测试密钥")
    parser.add_argument("--keystore", required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--bytes", dest="num_bytes", type=int, default=32)
    parser.add_argument(
        "--overwrite", action="store_true", help="覆盖已存在的同名 key id"
    )
    args = parser.parse_args(argv)

    try:
        secret = create_or_update_key(
            args.keystore, args.key_id,
            num_bytes=args.num_bytes, overwrite=args.overwrite,
        )
    except KeystoreError as exc:
        print(f"keygen: {exc}", file=sys.stderr)
        return 1

    print(f"已写入密钥 {args.key_id!r} -> {args.keystore}（权限 0600）")
    print(f"secret_hex={secret.hex()}")
    print("提醒：该密钥仅用于本地测试，请勿提交到版本库或用于生产。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
