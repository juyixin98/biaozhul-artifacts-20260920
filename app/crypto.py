"""真实密码学操作：HMAC-SHA256 请求签名与验签（hmac/hashlib 标准库实现）。

签名内容（防重放、防篡改、防路径混淆）：
    f"{method}\\n{path}\\n{timestamp}\\n{sha256_hex(raw_body)}"
传输头：
    X-Timestamp: 客户端当前 UNIX 秒
    X-Signature: hex( HMAC_SHA256(secret, canonical.encode()) )

验签使用 hmac.compare_digest 做常量时间比较；时间戳做新鲜度窗口校验。
"""
from __future__ import annotations

import hashlib
import hmac
import time

from .config import settings


def body_digest(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def canonical_message(method: str, path: str, timestamp: str, raw_body: bytes) -> bytes:
    return (
        f"{method.upper()}\n{path}\n{timestamp}\n{body_digest(raw_body)}"
    ).encode("utf-8")


def sign_request(
    method: str,
    path: str,
    raw_body: bytes,
    secret: str | None = None,
    timestamp: str | None = None,
) -> tuple[dict[str, str], str]:
    """客户端助手：返回需要附带的请求头与所用时间戳。"""
    ts = timestamp if timestamp is not None else f"{time.time():.6f}"
    mac = hmac.new(
        (secret or settings.HMAC_SECRET).encode("utf-8"),
        canonical_message(method, path, ts, raw_body),
        hashlib.sha256,
    )
    return {"X-Timestamp": ts, "X-Signature": mac.hexdigest()}, ts


def verify_request(
    method: str,
    path: str,
    timestamp: str | None,
    signature_hex: str | None,
    raw_body: bytes,
    *,
    now: float | None = None,
    secret: str | None = None,
    freshness_s: float | None = None,
) -> tuple[bool, str]:
    """返回 (是否通过, 原因)。原因仅用于日志/错误体，不泄露密钥。"""
    if timestamp is None or signature_hex is None:
        return False, "missing signature headers"
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        return False, "malformed timestamp"

    current = now if now is not None else time.time()
    window = freshness_s if freshness_s is not None else settings.SIGN_FRESHNESS_S
    if abs(current - ts) > window:
        return False, "stale or future timestamp"

    expected = hmac.new(
        (secret or settings.HMAC_SECRET).encode("utf-8"),
        canonical_message(method, path, timestamp, raw_body),
        hashlib.sha256,
    ).hexdigest()

    # 常量时间比较，防止计时侧信道
    if not hmac.compare_digest(expected, signature_hex.strip()):
        return False, "signature mismatch"
    return True, "ok"
