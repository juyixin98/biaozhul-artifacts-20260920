"""规范化 JSON（Canonical JSON）。

规则刻意做窄，保证"同一份数据无论字段顺序如何，签名字节完全一致"，
同时把容易引起歧义的输入直接拒掉：

1. 只允许对象、数组、字符串、整数、true/false、null；
   禁止浮点数、NaN/Infinity；
2. 顶层必须是对象；
3. **对象中出现重复键即报错**（不接受"后者覆盖前者"的默认行为）；
4. 字符串与键使用 NFC 归一化；归一化后产生冲突的键同样报错；
5. 对象键按 Unicode 码点排序；
6. 输出为 UTF-8 紧凑格式：无多余空白、键值间无空格；
7. 非 ASCII 字符直接输出（不转 \\uXXXX），只转义必须转义的字符；
8. 拒绝 UTF-8 BOM、拒绝非法代理项（lone surrogate）。

这与 RFC 8785 (JCS) 的关键做法一致，但额外禁用了数字中的浮点/指数形式，
以避免不同语言/版本间的数字序列化分歧。
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any, Tuple

from .errors import CanonicalJSONError

# RFC 8259 必须转义的字符，外加 DEL(0x7F) 与其余 C0/C1 控制字符。
# 引号、反斜杠使用短转义，其它控制字符使用 u00xx。
_SHORT_ESCAPES = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


def parse(text: str | bytes) -> Any:
    """严格解析 JSON 文本。

    - bytes 必须是无 BOM 的合法 UTF-8；
    - 重复对象键、NaN/Infinity、尾随内容一律报错；
    - 返回 Python 表示（dict 键保留 NFC 归一化之前的原样，
      归一化统一在 :func:`normalize` 中做，便于区分"重复键"与
      "归一化后冲突"两种情况，错误信息更准确）。
    """
    if isinstance(text, bytes):
        if text.startswith(b"\xef\xbb\xbf"):
            raise CanonicalJSONError("UTF-8 BOM 不允许出现在规范化 JSON 中")
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CanonicalJSONError(f"不是合法的 UTF-8: {exc}") from exc
    elif text.startswith("﻿"):
        raise CanonicalJSONError("UTF-8 BOM 不允许出现在规范化 JSON 中")

    pairs: list[list[tuple[str, Any]]] = []

    def _object_pairs_hook(items: list[tuple[str, Any]]) -> dict[str, Any]:
        # 在 hook 内立即检测重复键，不受 Python dict 去重影响。
        seen: set[str] = set()
        for key, _ in items:
            if key in seen:
                raise CanonicalJSONError(f"JSON 对象包含重复键: {key!r}")
            seen.add(key)
        pairs.append(items)
        return dict(items)

    def _reject_constant(value: str) -> Any:
        raise CanonicalJSONError(f"非法 JSON 常量: {value}")

    try:
        result = json.loads(
            text,
            object_pairs_hook=_object_pairs_hook,
            parse_constant=_reject_constant,
        )
    except CanonicalJSONError:
        raise
    except json.JSONDecodeError as exc:
        raise CanonicalJSONError(f"JSON 解析失败: {exc}") from exc

    return result


def _nfc(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    # 拒绝编码不出来的孤立代理项（json 模块编码时也会炸，提前给出明确错误）。
    normalized.encode("utf-8")
    return normalized


def _check_no_lone_surrogates(value: str, where: str) -> None:
    for ch in value:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            raise CanonicalJSONError(f"{where}包含非法的孤立代理项字符: U+{ord(ch):04X}")


def normalize(value: Any) -> Any:
    """对解析结果做 NFC 归一化与类型收窄，返回可安全编码的新对象。"""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # 整数形式的浮点也不行：1 与 1.0 必须有唯一表示。
        raise CanonicalJSONError("规范化 JSON 禁止浮点数，请改用整数或字符串")
    if isinstance(value, str):
        _check_no_lone_surrogates(value, "字符串")
        return _nfc(value)
    if isinstance(value, list) or isinstance(value, tuple):
        return [normalize(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, raw_val in value.items():
            if not isinstance(raw_key, str):
                raise CanonicalJSONError("对象键必须是字符串")
            _check_no_lone_surrogates(raw_key, "对象键")
            key = _nfc(raw_key)
            if key in result:
                raise CanonicalJSONError(
                    f"对象键经 NFC 归一化后冲突: {key!r}（原始键 {raw_key!r}）"
                )
            result[key] = normalize(raw_val)
        return result
    raise CanonicalJSONError(f"规范化 JSON 不支持的类型: {type(value).__name__}")


def _escape_string(value: str) -> str:
    out: list[str] = []
    for ch in value:
        code = ord(ch)
        short = _SHORT_ESCAPES.get(code)
        if short is not None:
            out.append(short)
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def encode(value: Any) -> bytes:
    """把已归一化的数据编码为确定性的紧凑 UTF-8 字节串。"""

    def _emit(node: Any, buf: list[str]) -> None:
        if node is None:
            buf.append("null")
        elif node is True:
            buf.append("true")
        elif node is False:
            buf.append("false")
        elif isinstance(node, int):
            buf.append(str(node))
        elif isinstance(node, str):
            buf.append(_escape_string(node))
        elif isinstance(node, list):
            buf.append("[")
            for i, item in enumerate(node):
                if i:
                    buf.append(",")
                _emit(item, buf)
            buf.append("]")
        elif isinstance(node, dict):
            buf.append("{")
            # Python sorted 按 Unicode 码点排序，符合要求。
            for i, key in enumerate(sorted(node.keys())):
                if i:
                    buf.append(",")
                buf.append(_escape_string(key))
                buf.append(":")
                _emit(node[key], buf)
            buf.append("}")
        else:  # 理论上 normalize 已拦掉，双保险。
            raise CanonicalJSONError(f"无法编码的类型: {type(node).__name__}")

    pieces: list[str] = []
    _emit(value, pieces)
    return "".join(pieces).encode("utf-8")


def canonical_bytes(value: Any) -> bytes:
    """归一化 + 编码，一步得到签名字节。"""
    return encode(normalize(value))


def canonicalize(text: str | bytes) -> Tuple[Any, bytes]:
    """解析、归一化并返回 (数据, 规范化字节)。"""
    data = normalize(parse(text))
    if not isinstance(data, dict):
        raise CanonicalJSONError("规范化 JSON 的顶层必须是对象")
    return data, encode(data)


def top_level_object_bytes(text: str | bytes) -> Tuple[dict, bytes]:
    """解析 JSON 文本，要求顶层为对象，并返回其规范化字节。

    签名 / 验签都走这里：先严格 parse（含重复键检测），再规范序列化，
    所以磁盘上的清单即使是美化排版、字段乱序，也能得到同一串字节。
    """
    data, raw = canonicalize(text)
    return data, raw
