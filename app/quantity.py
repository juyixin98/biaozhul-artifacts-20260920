"""Kubernetes 资源量（resource.Quantity 的明确子集）解析。

支持：
- 十进制 SI 后缀：n, u, m, "", k, M, G, T, P, E
- 二进制 SI 后缀：Ki, Mi, Gi, Ti, Pi, Ei
- 普通十进制数与定点小数（如 "1.5"、"500m"、"128Mi"）

不支持指数记法（如 "1e3"）——属于刻意裁剪的子集，解析时直接报错。
所有解析在本地真实执行，不依赖任何外部服务。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_DECIMAL_SUFFIXES = {
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "": Decimal("1"),
    "k": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
    "E": Decimal("1e18"),
}

_BINARY_SUFFIXES = {
    "Ki": Decimal(2**10),
    "Mi": Decimal(2**20),
    "Gi": Decimal(2**30),
    "Ti": Decimal(2**40),
    "Pi": Decimal(2**50),
    "Ei": Decimal(2**60),
}

_QUANTITY_RE = re.compile(r"^([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))([a-zA-Z]{0,2})$")


class QuantityParseError(ValueError):
    """资源量字符串无法解析。"""


def parse_quantity(raw: str) -> Decimal:
    """把 K8s 资源量字符串解析为基准单位的 Decimal（CPU 核、内存字节）。"""
    if not isinstance(raw, str):
        raise QuantityParseError(f"资源量必须是字符串，得到: {type(raw).__name__}")
    text = raw.strip()
    match = _QUANTITY_RE.match(text)
    if not match:
        raise QuantityParseError(f"无法解析的资源量: {raw!r}")
    number_text, suffix = match.groups()
    try:
        number = Decimal(number_text)
    except InvalidOperation as exc:  # pragma: no cover - 正则已保证可解析
        raise QuantityParseError(f"无法解析的数值部分: {raw!r}") from exc
    if suffix in _BINARY_SUFFIXES:
        return number * _BINARY_SUFFIXES[suffix]
    if suffix in _DECIMAL_SUFFIXES:
        return number * _DECIMAL_SUFFIXES[suffix]
    raise QuantityParseError(f"未知的资源量后缀: {raw!r}")


def cpu_to_milli(raw: str) -> int:
    """CPU 资源量 -> 毫核（millicores），向上取整到 1m 的整数倍。"""
    value = parse_quantity(raw)
    milli = value * 1000
    # 与 kubelet 一致：不足 1m 的非零值按 1m 计
    rounded = milli.to_integral_value(rounding="ROUND_CEILING")
    return int(rounded)


def memory_to_bytes(raw: str) -> int:
    """内存资源量 -> 字节，向上取整到整数字节。"""
    value = parse_quantity(raw)
    return int(value.to_integral_value(rounding="ROUND_CEILING"))
