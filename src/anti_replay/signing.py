"""签名串构造与校验（协议 v1）。

本模块只负责 “规范请求串 → HMAC” 这一层；
请求头解析、时间窗、nonce 登记由 :mod:`anti_replay.verifier` 串联。
"""

from __future__ import annotations

from .canonical import (
    build_canonical_request,
    canonical_request_target,
)
from .crypto import hmac_sha256_hex, verify_hmac

PROTOCOL_ID = "ANTI-REPLAY-API-HMAC-SHA256-v1"


def sign_canonical(canonical_request: str, key: bytes) -> str:
    """对已构造的规范请求串计算小写十六进制 HMAC-SHA256。"""
    return hmac_sha256_hex(key, canonical_request.encode("utf-8"))


def sign_request(
    *,
    key: bytes,
    key_id: str,
    method: str,
    target: str,
    body: bytes,
    timestamp: str,
    nonce: str,
) -> tuple[str, str, str, str]:
    """一站式签名助手（供客户端使用）。

    返回 ``(signature, canonical_request, canonical_path, canonical_query)``。
    """
    cpath, cquery = canonical_request_target(target)
    canonical_request = build_canonical_request(
        key_id=key_id,
        method=method,
        canonical_path_value=cpath,
        canonical_query_value=cquery,
        body=body,
        timestamp=timestamp,
        nonce=nonce,
    )
    return sign_canonical(canonical_request, key), canonical_request, cpath, cquery


def verify_request_signature(
    *,
    key: bytes,
    key_id: str,
    method: str,
    canonical_path_value: str,
    canonical_query_value: str,
    body: bytes,
    timestamp: str,
    nonce: str,
    signature_hex: str,
) -> bool:
    """重建规范请求串并做恒定时间 HMAC 比较。"""
    canonical_request = build_canonical_request(
        key_id=key_id,
        method=method,
        canonical_path_value=canonical_path_value,
        canonical_query_value=canonical_query_value,
        body=body,
        timestamp=timestamp,
        nonce=nonce,
    )
    return verify_hmac(key, canonical_request.encode("utf-8"), signature_hex)


__all__ = [
    "PROTOCOL_ID",
    "sign_canonical",
    "sign_request",
    "verify_request_signature",
]
