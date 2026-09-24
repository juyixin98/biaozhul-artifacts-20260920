"""基于 cryptography 的报告摘要与签名工具。

- sha256_hex: 计算规范 JSON 的 SHA-256 摘要，用于标识 SBOM/报告内容。
- hmac_sha256_hex: 用共享密钥对报告签名，便于下游校验报告未被篡改。
  密钥来自环境变量 SBOM_HMAC_KEY，缺省使用仅供本地开发的弱密钥。
"""
from __future__ import annotations

import json
import os

from cryptography.hazmat.primitives import hashes, hmac

DEFAULT_HMAC_KEY = b"dev-insecure-key-change-me"


def canonical_json(obj) -> bytes:
    """生成规范的 JSON 字节串（键排序、无空白），保证摘要稳定。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    digest = hashes.Hash(hashes.SHA256())
    digest.update(data)
    return digest.finalize().hex()


def hmac_sha256_hex(key: bytes, data: bytes) -> str:
    signer = hmac.HMAC(key, hashes.SHA256())
    signer.update(data)
    return signer.finalize().hex()


def sign_report(report: dict) -> str:
    """对报告（不含签名字段）计算 HMAC-SHA256 签名。"""
    key = os.environ.get("SBOM_HMAC_KEY", "").encode("utf-8") or DEFAULT_HMAC_KEY
    return hmac_sha256_hex(key, canonical_json(report))
