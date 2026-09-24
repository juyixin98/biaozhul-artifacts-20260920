"""真实的 HMAC-SHA256 请求/响应签名（非演示桩）。

启用方式：设置环境变量 ``IMU_BIAS_HMAC_SECRET``（也可用 ``IMU_BIAS_HMAC_SECRET_FILE``
指向含密钥的文件）。未配置时鉴权关闭，健康检查接口始终开放。

请求签名
--------
客户端对字符串 ``f"{timestamp}.{method}.{path}.{sha256(body)}"`` 用密钥计算
HMAC-SHA256，发送：
- ``X-Timestamp``：Unix 秒（允许小数）
- ``X-Signature``：``sha256=<hex>``

服务端：
1. 时间戳与服务器时间相差超过 ``MAX_SKEW_S`` 秒 -> 401（防重放）；
2. 用 ``hmac.compare_digest`` 常量时间比较重算签名 -> 不符即 401。

响应签名（供客户端校验响应完整性）
--------------------------------
响应头 ``X-Response-Signature`` 为 ``sha256=<HMAC(secret, response_body)>``。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from .errors import AuthError

MAX_SKEW_S = 300.0
SCHEME = "sha256"


def get_secret() -> bytes | None:
    secret = os.environ.get("IMU_BIAS_HMAC_SECRET")
    if secret is not None:
        return secret.encode("utf-8")
    path = os.environ.get("IMU_BIAS_HMAC_SECRET_FILE")
    if path:
        with open(path, "rb") as fh:
            data = fh.read().strip()
        return data or None
    return None


def signing_payload(timestamp: str, method: str, path: str, body: bytes) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{timestamp}.{method.upper()}.{path}.{body_hash}".encode("utf-8")


def compute_signature(secret: bytes, payload: bytes) -> str:
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def sign(timestamp: str, method: str, path: str, body: bytes, secret: bytes) -> str:
    return f"{SCHEME}={compute_signature(secret, signing_payload(timestamp, method, path, body))}"


def verify_request(
    method: str,
    path: str,
    body: bytes,
    timestamp_header: str | None,
    signature_header: str | None,
    secret: bytes,
    now: float | None = None,
) -> None:
    """校验失败抛 :class:`AuthError`。"""
    if not timestamp_header or not signature_header:
        raise AuthError(
            "缺少签名头 X-Timestamp / X-Signature（服务端已启用 HMAC 鉴权）"
        )
    try:
        ts = float(timestamp_header)
    except ValueError:
        raise AuthError("X-Timestamp 不是合法的 Unix 秒") from None

    now = time.time() if now is None else now
    if abs(now - ts) > MAX_SKEW_S:
        raise AuthError(
            f"请求时间戳偏差超过 {MAX_SKEW_S:.0f}s，可能为重放请求",
            {"server_time": now, "client_time": ts},
        )

    if not signature_header.startswith(f"{SCHEME}="):
        raise AuthError(f"签名方案必须为 {SCHEME}=<hex>")
    provided = signature_header[len(SCHEME) + 1 :].strip()
    expected = compute_signature(secret, signing_payload(timestamp_header, method, path, body))
    if not hmac.compare_digest(provided, expected):
        raise AuthError("HMAC 签名校验失败")
