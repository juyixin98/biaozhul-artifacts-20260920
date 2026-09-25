"""规范化序列化与 Ed25519 签名。

canonical JSON：UTF-8、键按字典序、无空白、``ensure_ascii=False``、
无整数之外的浮点数（导出器会拒绝浮点策略参数；记录数据中的浮点
保留 JSON repr，但签名仍对其规范化输出）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SIGNATURE_VERSION = "mde/signature@v1"
CANONICAL_VERSION = "mde/canonical-json@v1"


def canonical(obj: Any) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(obj: Any) -> str:
    """对规范化内容取 SHA256 指纹。"""
    return sha256_hex(canonical(obj))


def sign(priv: Ed25519PrivateKey, obj: Any) -> dict:
    body = canonical(obj)
    sig = priv.sign(body)
    return {
        "version": SIGNATURE_VERSION,
        "alg": "Ed25519",
        "canonical": CANONICAL_VERSION,
        "covered_fields": list(obj.keys()) if isinstance(obj, dict) else [],
        "value_hex": sig.hex(),
    }


def verify(pub: Ed25519PublicKey, obj: Any, signature: dict) -> tuple[bool, str]:
    """返回 (是否通过, 原因说明)。签名值以外的字段必须逐项核对。"""
    if not isinstance(signature, dict):
        return False, "签名不是对象"
    if signature.get("version") != SIGNATURE_VERSION:
        return False, f"签名版本不支持: {signature.get('version')!r}"
    if signature.get("alg") != "Ed25519":
        return False, f"签名算法不支持: {signature.get('alg')!r}"
    if signature.get("canonical") != CANONICAL_VERSION:
        return False, "规范化算法不匹配"
    covered = signature.get("covered_fields")
    expected = sorted(obj.keys()) if isinstance(obj, dict) else []
    if sorted(covered or []) != expected:
        return False, "签名覆盖字段与被签对象不一致"
    try:
        sig_bytes = bytes.fromhex(signature.get("value_hex", ""))
    except (TypeError, ValueError):
        return False, "签名值不是 hex"
    try:
        pub.verify(sig_bytes, canonical(obj))
    except InvalidSignature:
        return False, "Ed25519 验签失败"
    return True, "ok"
