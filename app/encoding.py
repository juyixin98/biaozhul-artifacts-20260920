# -*- coding: utf-8 -*-
"""编码工具：十六进制、base64、定长整数以及规范 JSON 序列化。

教学说明：IBC 真实实现中，状态值（承诺、序列号、回执）都使用确定性的
字节编码，本模块用同样的规则执行真实编码，而非字符串拼接。
"""
from __future__ import annotations

import base64
import json
from typing import Any


def to_hex(b: bytes) -> str:
    return b.hex()


def from_hex(s: str | bytes, name: str = "value") -> bytes:
    if isinstance(s, bytes):
        return s
    if not isinstance(s, str):
        raise ValueError(f"{name} 必须是十六进制字符串")
    try:
        return bytes.fromhex(s.strip())
    except ValueError as exc:
        raise ValueError(f"{name} 不是合法十六进制: {exc}") from exc


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def ub64(s: str, name: str = "value") -> bytes:
    try:
        return base64.b64decode(s, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{name} 不是合法 base64: {exc}") from exc


def u64be(n: int) -> bytes:
    """无符号 64 位大端整数（IBC 对 nextSequenceRecv 等使用的编码）。"""
    if not isinstance(n, int) or isinstance(n, bool):
        raise ValueError("期望整数")
    if n < 0 or n > 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"超出 uint64 范围: {n}")
    return n.to_bytes(8, "big")


def u64be_to_int(b: bytes) -> int:
    if len(b) != 8:
        raise ValueError(f"uint64 必须为 8 字节，实际 {len(b)}")
    return int.from_bytes(b, "big")


def canonical_json(obj: Any) -> bytes:
    """规范 JSON：键按字典序排序、无空白、保留中文、键值分隔符固定。

    用作通道状态值与检查点元数据的确定性编码。
    """
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
