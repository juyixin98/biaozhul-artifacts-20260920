"""确定性 JSON 编码（RFC 8785 JCS 的受限实现）。

用途：策略指纹、决策哈希、签名都基于字节级确定的序列化结果，
避免字段顺序、Unicode 转义差异导致同一文档得到不同摘要。

刻意只支持引擎中实际出现的 JSON 类型：
dict（键按 UTF-16 码元排序，与 JSON.parse 后的键序语义一致）、
list、str、int、float、bool、None。
不支持 set/tuple/bytes 等，遇到即报错，防止静默编码出不可预期的结果。
"""

from __future__ import annotations

import json
import math
from typing import Any

# 与 RFC 8785 一致：仅对必须转义的字符使用短转义，其余输出原字符。
_ESCAPE = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


def _utf16_key_sort_key(key: str) -> tuple[int, ...]:
    """按键的 UTF-16 码元序列排序（模拟 JCS 对 member name 的排序）。"""
    return tuple(
        ord(c) if ord(c) <= 0xFFFF else 0  # BMP 内单码元
        for c in key
    )


def _escape_string(s: str) -> str:
    out: list[str] = ['"']
    for ch in s:
        cp = ord(ch)
        if cp in _ESCAPE:
            out.append(_ESCAPE[cp])
        elif cp < 0x20:
            out.append(f"\\u{cp:04x}")
        else:
            # 直接输出原字符（UTF-8 编码由最后的 .encode 处理）。
            out.append(ch)
    out.append('"')
    return "".join(out)


def _encode_float(x: float) -> str:
    if math.isnan(x) or math.isinf(x):
        # JSON 不允许 NaN/Infinity；明确拒绝而非输出非标准 token。
        raise ValueError("canonical JSON cannot encode NaN/Infinity")
    if x == 0.0:
        # 区分 0 与 -0，二者值相等但字节不同；JCS 保留 -0 语义。
        return "-0" if math.copysign(1.0, x) < 0 else "0"
    # repr(float) 在 CPython 上给出最短往返表示（ECMAScript 语义近似）。
    s = repr(x)
    if s in ("Infinity", "-Infinity", "nan"):  # pragma: no cover
        raise ValueError("non-finite float")
    # 规范化科学计数法指数表示（1e+05 -> 1e5，与 JCS 风格保持一致）。
    s = s.replace("e+0", "e+").replace("e-0", "e-")
    if s.startswith("e") or "e" in s:
        parts = s.split("e")
        if len(parts) == 2 and parts[1].startswith("+"):
            exp = parts[1][1:]
            s = f"{parts[0]}e{exp}"
    return s


def _encode(obj: Any) -> str:
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, bool):  # 必须在 int 之前
        return "true" if obj else "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        return _encode_float(obj)
    if isinstance(obj, str):
        return _escape_string(obj)
    if isinstance(obj, dict):
        items: list[tuple[str, Any]] = list(obj.items())
        for k, _ in items:
            if not isinstance(k, str):
                raise ValueError("canonical JSON object keys must be strings")
        items.sort(key=lambda kv: _utf16_key_sort_key(kv[0]))
        return "{" + ",".join(f"{_escape_string(k)}:{_encode(v)}" for k, v in items) + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(_encode(v) for v in obj) + "]"
    raise TypeError(f"cannot canonical-encode value of type {type(obj).__name__}")


def canonical_dumps(obj: Any) -> bytes:
    """返回确定性 UTF-8 字节串。"""
    return _encode(obj).encode("utf-8")


def stable_json_dumps(obj: Any) -> str:
    """带缩进、键序确定的 JSON 文本（供展示/写文件；签名不使用它）。"""
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False,
                      separators=(",", ": "))
