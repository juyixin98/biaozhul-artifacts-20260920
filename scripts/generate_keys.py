#!/usr/bin/env python3
"""生成 Ed25519 密钥对（PEM）。

用法:
    python scripts/generate_keys.py [输出目录] [kid]

默认输出到 keys/：dev_private.pem（PKCS8，不加密）与 dev_public.pem（SPKI）。
生产环境请将私钥放入密钥管理系统，切勿沿用开发密钥。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.crypto import generate_keypair, private_key_to_pem, public_key_to_pem


def main() -> int:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "keys"
    kid = sys.argv[2] if len(sys.argv) > 2 else "2026-09-dev"

    os.makedirs(out_dir, exist_ok=True)
    priv, pub = generate_keypair()
    priv_path = os.path.join(out_dir, "dev_private.pem")
    pub_path = os.path.join(out_dir, "dev_public.pem")

    with open(priv_path, "wb") as fh:
        fh.write(private_key_to_pem(priv))
    os.chmod(priv_path, 0o600)
    with open(pub_path, "wb") as fh:
        fh.write(public_key_to_pem(pub))

    print(f"kid = {kid}")
    print(f"private key -> {priv_path} (0600)")
    print(f"public key  -> {pub_path}")
    print("提示: 设置 POLICY_TRUSTED_PUBLIC_KEY=" + pub_path + " 后重启服务即可验签。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
