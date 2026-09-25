"""规范化 JSON (Canonical JSON) —— 明确、成文的规则集。

本模块*不*自创加密算法; 它只负责把 JSON 文档确定性地字节化,
使同一份语义数据在任何机器上得到完全相同的 UTF-8 字节,
签名/摘要就是针对这串字节计算的。

规则 (canonical rules, 全部为显式定义, 不依赖 json.dumps 的默认行为):

1. 编码固定为 UTF-8; 输入若带 BOM 一律拒绝 (不静默剥离)。
2. 只允许 JSON 标准类型: object / array / string / number / true / false / null。
   - 解析层禁止 NaN / Infinity / -Infinity;
   - 解析层禁止对象内出现重复键 (RFC 8259 §4 允许但语义不明确, 这里从严);
   - 解析层禁止尾部空白之外的多余字节;
   - 顶层必须是对象或数组 (防止歧义的裸标量)。
3. 对象成员按键排序: 以键的 Unicode 码位 (code point) 比较;
   对象内不允许出现重复键 (已在规则 2 保证)。
4. 空白最小化: 分隔符 `,` `:` 两侧无空白; 数组/对象内部无空白。
5. 字符串:
   - 非 ASCII 字符直接以 UTF-8 原样输出 (不转义 \\uXXXX),
     因此含非 ASCII 的文档在字节级与编码绑定 (固定 UTF-8, 无歧义);
   - 仅转义: 双引号 \\\\, U+0000..U+001F 控制字符;
   - 控制字符中除 JSON 8 个短转义 (" \\b \\f \\n \\r \\t) 外,
     一律使用 \\u00XX 形式 (4 位十六进制小写)。
6. 数字:
   - 整数按最短十进制输出 (Python int 任意精度, 不使用科学计数法);
   - 有限浮点使用 Python repr 的最短往返表示; 非有限值拒绝;
   - -0.0 输出 -0.0;
   - 布尔/null 不视作数字。
7. 禁止 Python 端的 set / tuple / bytes / datetime 等非 JSON 类型直接进入序列化。
8. 规范化输出确定性: 同一输入多次编码字节完全一致; 且
   canonical(parse(x)) == canonical(parse(canonical(parse(x)))) (往返稳定)。

注意: 本方案与 RFC 8785 (JCS) 的区别是键序使用 Unicode 码位而非
UTF-16 码元序; 由于本系统签名与验签都使用本模块, 互操作不受影响,
此处有意选择更简单且在纯 Python 下可直接复现的规则。
"""

from __future__ import annotations

import json

from .errors import CanonicalJSONError

# JSON 允许的 8 个短转义 (其余控制字符用 \u00xx)
_SHORT_ESCAPES = {
    0x08: r"\b",
    0x09: r"\t",
    0x0A: r"\n",
    0x0C: r"\f",
    0x0D: r"\r",
    0x22: r"\"",
    0x5C: r"\\",
}

_BOM = "﻿"


def _reject_constants(value: str) -> None:
    """parse_constant 回调: 命中 NaN / Infinity / -Infinity 即报错。"""
    raise CanonicalJSONError(f"非法的 JSON 常量: {value!r} (禁止 NaN/Infinity)")


class _DuplicateKeyDetector(dict):
    """dict 子类: 构造时若遇到重复键立即报错 (Python 3.7+ dict 保持插入序)。"""

    def __init__(self, pairs):
        super().__init__()
        for key, value in pairs:
            if key in self:
                raise CanonicalJSONError(f"对象包含重复的键: {key!r}")
            self[key] = value


def _encode_string(s: str, out: list[str]) -> None:
    """按规则 5 编码 JSON 字符串。"""
    out.append('"')
    for ch in s:
        cp = ord(ch)
        if cp in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[cp])
        elif cp < 0x20:
            out.append("\\u%04x" % cp)
        else:
            # 含孤立代理项 (lone surrogate) 的字符串无法编码为合法 UTF-8
            try:
                ch.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise CanonicalJSONError(
                    f"字符串包含无法以 UTF-8 编码的字符: U+{cp:04X}"
                ) from exc
            out.append(ch)
    out.append('"')


def _encode_number(n, out: list[str]) -> None:
    """按规则 6 编码数字。"""
    if isinstance(n, bool):  # bool 是 int 的子类, 先排除
        raise CanonicalJSONError("布尔值不得作为数字")
    if isinstance(n, int):
        out.append(str(n))
        return
    if isinstance(n, float):
        if n != n or n in (float("inf"), float("-inf")):
            raise CanonicalJSONError("禁止非有限浮点 (NaN/Infinity)")
        text = repr(n)
        # 保险: 确认产出是合法 JSON 数字片段
        if any(c in text for c in "nN") or text in {"inf", "-inf"}:
            raise CanonicalJSONError(f"无法规范化浮点: {n!r}")
        out.append(text)
        return
    raise CanonicalJSONError(f"不支持的数字类型: {type(n).__name__}")


def _encode(value, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        _encode_string(value, out)
    elif isinstance(value, int) and not isinstance(value, bool):
        _encode_number(value, out)
    elif isinstance(value, float):
        _encode_number(value, out)
    elif isinstance(value, dict):
        # 规则 3: 键按 Unicode 码位排序
        keys = list(value.keys())
        if any(not isinstance(k, str) for k in keys):
            raise CanonicalJSONError("对象键必须全部为字符串")
        if len(set(keys)) != len(keys):
            dup = next(k for k in keys if keys.count(k) > 1)
            raise CanonicalJSONError(f"对象包含重复的键: {dup!r}")
        out.append("{")
        first = True
        for key in sorted(keys):
            if not first:
                out.append(",")
            first = False
            _encode_string(key, out)
            out.append(":")
            _encode(value[key], out)
        out.append("}")
    elif isinstance(value, list):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _encode(item, out)
        out.append("]")
    else:
        raise CanonicalJSONError(
            f"类型 {type(value).__name__} 不属于 JSON 标准类型, 拒绝规范化"
        )


def canonical_bytes(value) -> bytes:
    """把已解析的 Python JSON 值规范化为 UTF-8 字节。"""
    out: list[str] = []
    _encode(value, out)
    return "".join(out).encode("utf-8")


def canonical_text(value) -> str:
    """同 :func:`canonical_bytes`, 返回 str。"""
    return canonical_bytes(value).decode("utf-8")


def parse_strict(data: bytes | str):
    """严格解析 JSON: 拒绝 BOM / 重复键 / NaN-Infinity / 多余字节 / 非容器顶层。

    返回解析后的 Python 对象。
    """
    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CanonicalJSONError("输入不是合法 UTF-8") from exc
    else:
        text = data

    if text.startswith(_BOM):
        raise CanonicalJSONError("输入以 BOM 开头, 拒绝处理")

    try:
        value, end = json.JSONDecoder(
            object_pairs_hook=_DuplicateKeyDetector,
            parse_constant=_reject_constants,
        ).raw_decode(text)
    except json.JSONDecodeError as exc:
        raise CanonicalJSONError(f"JSON 解析失败: {exc.msg} (位置 {exc.pos})") from exc

    # raw_decode 之后只允许空白
    if text[end:].strip():
        raise CanonicalJSONError("文档末尾存在多余字节")

    if not isinstance(value, (dict, list)):
        raise CanonicalJSONError("顶层必须是对象或数组")

    return value


def canonicalize(data: bytes | str) -> bytes:
    """解析 + 规范化一步到位: 任意 JSON 字节 -> 规范字节。"""
    return canonical_bytes(parse_strict(data))


def pretty_json(value) -> bytes:
    """生成人类可读的、确定性的 JSON (仅用于清单文件落盘, 不用于签名)。

    使用 2 空格缩进; 同样拒绝 NaN/重复键等, 保证可读文件依然严格。
    """
    # 先走一遍 canonical 编码器, 触发同样的类型/重复键检查
    canonical_bytes(value)  # 仅校验
    text = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
        separators=(",", ": "),
        allow_nan=False,
    )
    return (text + "\n").encode("utf-8")
