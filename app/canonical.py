"""规范化 JSON（DSSE 之外的经典 TUF 签名负载编码）。

签名与验证必须对**完全相同的字节**进行，因此 ``signed`` 段落使用
确定性的 JSON 编码：

* 键按 UTF-16 码点排序（等价于按 Unicode 码点排序）；
* 不保留键之间、元素之间的空白；
* ``/`` 不转义（``URIESCAPE=False``），非 ASCII 字符不转义；
* 只允许有限的 JSON 类型，禁止浮点与字节串，避免同一数值出现多种编码。
"""

from __future__ import annotations

import json
from typing import Any

from .errors import MetadataError

ALLOWED_TYPES = (dict, list, str, int, bool, type(None))


def canonical(obj: Any) -> bytes:
    """返回 TUF 规范化 JSON 字节串。"""

    return _encode(obj).encode("utf-8")


def _encode(obj: Any) -> str:
    # bool 是 int 的子类，必须先判断
    if obj is None or isinstance(obj, bool):
        return json.dumps(obj, separators=(",", ":"))
    if isinstance(obj, int):
        return str(int(obj))
    if isinstance(obj, str):
        return _json_string(obj)
    if isinstance(obj, list):
        return "[" + ",".join(_encode(item) for item in obj) + "]"
    if isinstance(obj, dict):
        for key in obj:
            if not isinstance(key, str):
                raise MetadataError("规范化 JSON 的对象键必须是字符串")
        items = sorted(obj.items(), key=lambda kv: kv[0].encode("utf-16-be"))
        return "{" + ",".join(
            _json_string(key) + ":" + _encode(value) for key, value in items
        ) + "}"
    raise MetadataError(f"规范化 JSON 不支持类型: {type(obj).__name__}")


def _json_string(text: str) -> str:
    # 与 TUF 参考实现一致：只转义 RFC 8259 要求的字符，保留 UTF-8 与 "/"
    out = ['"']
    for ch in text:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)
