"""密码学操作：池快照摘要与报价 HMAC 签名（全部真实执行，无桩）。

* ``snapshot_hash``：对池快照的**规范 JSON**（键排序、无空白、ensure_ascii=False）
  计算 SHA-256，把每次报价绑定到不可变快照。
* ``sign_quote``：对报价负载的规范 JSON 计算 HMAC-SHA256，密钥来自
  环境变量 ``CLMM_HMAC_KEY``（未设置时使用开发默认值并打印告警；
  生产环境必须覆盖）。
* ``verify_quote``：用 ``hmac.compare_digest`` 常量时间比较，防时序侧信道。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

_DEV_KEY = b"dev-only-clmm-hmac-key-do-not-use-in-prod"


def canonical_json(obj: Any) -> bytes:
    """规范序列化：键排序、无多余空白、不转义非 ASCII。"""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_hash(snapshot: dict) -> str:
    """池快照内容的 SHA-256（十六进制）。"""
    return sha256_hex(canonical_json(snapshot))


def get_hmac_key() -> bytes:
    key = os.environ.get("CLMM_HMAC_KEY")
    if key:
        return key.encode("utf-8")
    return _DEV_KEY


def using_dev_key() -> bool:
    return "CLMM_HMAC_KEY" not in os.environ


def sign_quote(payload: dict) -> str:
    """对报价负载（已包含 snapshot_hash）做 HMAC-SHA256，返回十六进制摘要。"""
    return hmac.new(get_hmac_key(), canonical_json(payload), hashlib.sha256).hexdigest()


def verify_quote(payload: dict, signature: str) -> bool:
    """常量时间校验签名。签名或负载类型非法时返回 False，不抛异常。"""
    if not isinstance(signature, str):
        return False
    try:
        expected = sign_quote(payload)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature)
