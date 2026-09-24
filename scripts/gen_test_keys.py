#!/usr/bin/env python3
"""生成整套**测试用** Ed25519 密钥（切勿用于生产）。

用法::

    python scripts/gen_test_keys.py [输出目录]   # 默认 dev-keys/

产物（JSON）：
  root_threshold_keys.json  旧根 3 个阈值成员（私钥+公钥+key_id）
  signer_keys.json          2 个制品签名密钥
  new_threshold_keys.json   轮换后新根的 2 个阈值成员
  new_signer_keys.json      轮换后新根的 1 个制品签名密钥
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.crypto import generate_keypair


def _dump(path: str, keys: list) -> None:
    payload = [
        {"key_id": k.kid, "private_key_hex": k.private_key_hex, "public_key_hex": k.public_key_hex}
        for k in keys
    ]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"wrote {path} ({len(payload)} keys)")


def main() -> int:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "dev-keys"
    os.makedirs(out_dir, exist_ok=True)
    _dump(os.path.join(out_dir, "root_threshold_keys.json"), [generate_keypair() for _ in range(3)])
    _dump(os.path.join(out_dir, "signer_keys.json"), [generate_keypair() for _ in range(2)])
    _dump(os.path.join(out_dir, "new_threshold_keys.json"), [generate_keypair() for _ in range(2)])
    _dump(os.path.join(out_dir, "new_signer_keys.json"), [generate_keypair() for _ in range(1)])
    print(f"\n全部密钥仅用于测试，输出目录：{out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
