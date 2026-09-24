"""生成夹具用 Ed25519 密钥对（仅用于本地测试，绝非生产密钥）。

用法: python -m scripts.gen_test_keys
输出: fixtures/keys/test_feed_private.pem 和 test_feed_public.pem
"""
from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

KEYS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "keys"
KID = "test-fixture-key-2026"


def main() -> None:
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    priv_path = KEYS_DIR / "test_feed_private.pem"
    pub_path = KEYS_DIR / "test_feed_public.pem"

    if priv_path.exists() and pub_path.exists():
        print(f"密钥已存在，跳过: {priv_path}")
        return

    private_key = Ed25519PrivateKey.generate()
    priv_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    print(f"已生成测试密钥对 (kid={KID}):")
    print(f"  私钥(保密, 仅夹具签名用): {priv_path}")
    print(f"  公钥(服务验证用):         {pub_path}")


if __name__ == "__main__":
    main()
