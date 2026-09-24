"""HMAC-SHA256 请求签名鉴权（真实密码学操作，hmac/hashlib，非占位）。

签名协议：
  客户端与服务端共享密钥 IK_API_KEY。
  请求头：
    X-IK-Key-Id     密钥标识（当前固定 "default"，便于将来轮换）
    X-IK-Timestamp  Unix 秒时间戳（容差 ±IK_TIMESTAMP_TOLERANCE，防重放）
    X-IK-Nonce      客户端随机串（服务端在时间窗内去重，防重放）
    X-IK-Signature  hex(HMAC_SHA256(key, f"{key_id}\\n{timestamp}\\n{nonce}\\n{body_sha256}"))
  其中 body_sha256 是原始请求字节（UTF-8）的 SHA-256 hex，
  签名把请求体绑定进 MAC，防止路径外篡改。

  比较使用 hmac.compare_digest（恒定时间，防时序侧信道）。

服务启动时若 IK_REQUIRE_AUTH=true（默认）且未配置 IK_API_KEY，直接失败，
绝不以“空密钥放行”的方式运行。
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import OrderedDict

from fastapi import Header, HTTPException, Request, status

from .config import CryptoConfig


class NonceCache:
    """时间窗内已用 nonce 的有界缓存（LRU + 过期）。"""

    def __init__(self, ttl: float, capacity: int = 10000) -> None:
        self._ttl = ttl
        self._capacity = capacity
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def check_and_add(self, nonce: str, now: float) -> bool:
        """返回 True 表示新鲜；已见过则 False。顺带清理过期项。"""
        with self._lock:
            expired_before = now - 2.0 * self._ttl
            for k in list(self._seen.keys()):
                if self._seen[k] < expired_before:
                    self._seen.popitem(last=False)
                else:
                    break
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            self._seen.move_to_end(nonce)
            while len(self._seen) > self._capacity:
                self._seen.popitem(last=False)
            return True


def compute_signature(
    key: str, key_id: str, timestamp: str, nonce: str, raw_body: bytes
) -> str:
    body_sha = hashlib.sha256(raw_body).hexdigest()
    message = f"{key_id}\n{timestamp}\n{nonce}\n{body_sha}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_signature_headers(
    key_id: str | None,
    timestamp: str | None,
    nonce: str | None,
    signature: str | None,
    raw_body: bytes,
    cfg: CryptoConfig,
    nonce_cache: NonceCache,
    now: float | None = None,
) -> None:
    """校验失败抛 401；通过返回 None。"""
    if not cfg.require_auth:
        return
    if not cfg.api_key:
        # 配置错误属于服务端责任：500.0.1 而不是放行
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="服务器未配置 IK_API_KEY 却启用了鉴权，拒绝服务以防空密钥放行",
        )
    missing = [
        n
        for n, v in (
            ("X-IK-Key-Id", key_id),
            ("X-IK-Timestamp", timestamp),
            ("X-IK-Nonce", nonce),
            ("X-IK-Signature", signature),
        )
        if not v
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"缺少鉴权头: {', '.join(missing)}",
        )

    if key_id != "default":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未知 key id")

    try:
        ts = float(timestamp)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="X-IK-Timestamp 非法"
        )
    now = time.time() if now is None else now
    if abs(now - ts) > cfg.timestamp_tolerance:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"时间戳超出 ±{cfg.timestamp_tolerance:.0f}s 容差（可能为重放）",
        )

    expected = compute_signature(cfg.api_key, key_id, timestamp, nonce, raw_body)  # type: ignore[arg-type]
    if not hmac.compare_digest(expected, signature or ""):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="签名校验失败"
        )

    if not nonce_cache.check_and_add(nonce, now):  # type: ignore[arg-type]
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="nonce 已使用（拒绝重放）"
        )


async def auth_dependency(
    request: Request,
    x_ik_key_id: str | None = Header(default=None),
    x_ik_timestamp: str | None = Header(default=None),
    x_ik_nonce: str | None = Header(default=None),
    x_ik_signature: str | None = Header(default=None),
) -> None:
    cfg: CryptoConfig = request.app.state.crypto_config
    cache: NonceCache = request.app.state.nonce_cache
    raw_body = await request.body()
    verify_signature_headers(
        x_ik_key_id,
        x_ik_timestamp,
        x_ik_nonce,
        x_ik_signature,
        raw_body,
        cfg,
        cache,
    )
