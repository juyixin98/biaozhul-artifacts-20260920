"""密码学操作（全部真实执行，使用标准库 secrets/hashlib/hmac）。

- 地图版本内容哈希：SHA-256 over 规范化 JSON（确定性、可复算）。
- 预订撤销凭证：服务端只保存 ``secrets.token_hex`` 凭证的 SHA-256 哈希；
  撤销时用 ``hmac.compare_digest`` 做常量时间比较，避免时序侧信道。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets


def random_token(num_bytes: int = 32) -> str:
    """生成加密安全的随机撤销凭证（URL 安全十六进制）。"""
    return secrets.token_hex(num_bytes)


def hash_token(token: str) -> str:
    """对撤销凭证做 SHA-256，落库的永远是哈希而非凭证本身。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, stored_hash: str) -> bool:
    """常量时间比较凭证哈希。"""
    return hmac.compare_digest(hash_token(token), stored_hash)


def canonical_map_json(
    width: int,
    height: int,
    obstacles: list[tuple[int, int]],
) -> bytes:
    """规范化地图序列化：键排序、无多余空白，保证哈希可复算。"""
    payload = {
        "width": width,
        "height": height,
        "obstacles": sorted([list(c) for c in obstacles]),
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def content_hash(
    width: int,
    height: int,
    obstacles: list[tuple[int, int]],
) -> str:
    """地图内容的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(canonical_map_json(width, height, obstacles)).hexdigest()
