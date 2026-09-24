"""响应完整性签名：真实执行 SHA-256 与 HMAC-SHA256（无桩、无占位）。

密钥来源（按优先级）:
  1. 环境变量 RTA_HMAC_KEY（显式配置，>=16 字节，供多副本/客户端验签）；
  2. 未配置时在进程启动时用 secrets.token_bytes(32) 生成一次性临时密钥，
     仅本进程存活期间有效——重启即失效，属刻意设计而非缺陷。

签名对象：响应体去掉 sha256 / hmac_sha256 两个字段后的规范 JSON
（sort_keys、紧凑分隔、UTF-8、ensure_ascii=False）。
验签方按同样规则重算即可。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets

_MIN_KEY_LEN = 16


def load_secret_key() -> tuple[bytes, str]:
    raw = os.environ.get("RTA_HMAC_KEY")
    if raw:
        key = raw.encode("utf-8")
        if len(key) < _MIN_KEY_LEN:
            raise RuntimeError(
                f"RTA_HMAC_KEY 至少需要 {_MIN_KEY_LEN} 字节，当前 {len(key)} 字节"
            )
        return key, "env:RTA_HMAC_KEY"
    return secrets.token_bytes(32), "ephemeral:secrets.token_bytes(32)"


SECRET_KEY, KEY_SOURCE = load_secret_key()


def _canonical_bytes(payload: dict) -> bytes:
    body = dict(payload)
    body.pop("sha256", None)
    body.pop("hmac_sha256", None)
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sign(payload: dict) -> tuple[str, str]:
    """返回 (sha256_hex, hmac_sha256_hex)，就地不修改 payload。"""
    blob = _canonical_bytes(payload)
    digest = hashlib.sha256(blob).hexdigest()
    mac = hmac.new(SECRET_KEY, blob, hashlib.sha256).hexdigest()
    return digest, mac


def verify(payload: dict, provided_hmac: str) -> bool:
    blob = _canonical_bytes(payload)
    expected = hmac.new(SECRET_KEY, blob, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided_hmac)
