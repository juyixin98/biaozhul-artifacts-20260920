"""字节码十六进制处理、库占位识别、半字节（nibble）级掩码与差异分析。

solc 的 linkReferences 偏移量单位是 **半字节**（每个 hex 字符一位），
库地址占 40 个 hex 字符；因此所有比较都在 nibble 层进行，确保差异不会被
字节对齐“吞掉”。
"""

from __future__ import annotations

import re

#: 新版占位：__$<34 个 0-9a-zA-Z>$__（总长 40 hex 字符，作为半字节串）
_PLACEHOLDER_NEW = re.compile(r"__\$[0-9A-Za-z]{34}\$__")
#: 旧版占位：__<名字补下划线到 40 字符>__
_PLACEHOLDER_LEGACY = re.compile(r"__[A-Za-z0-9_]{36}__")


class BytecodeError(ValueError):
    """字节码十六进制非法。"""


def normalize_hex(value: str | None, *, allow_empty: bool = False) -> str:
    """去除 0x 前缀并校验为偶数长度纯十六进制；返回小写 hex。"""
    if value is None or value == "":
        if allow_empty:
            return ""
        raise BytecodeError("字节码为空")
    if not isinstance(value, str):
        raise BytecodeError("字节码必须是字符串")
    s = value.strip()
    if s.startswith("0x") or s.startswith("0X"):
        s = s[2:]
    if s == "":
        if allow_empty:
            return ""
        raise BytecodeError("字节码为空")
    if len(s) % 2 != 0:
        raise BytecodeError(f"字节码十六进制长度为奇数: {len(s)}")
    if not re.fullmatch(r"[0-9a-fA-F]+", s):
        raise BytecodeError("字节码包含非十六进制字符")
    return s.lower()


def normalize_unlinked_hex(value: str | None) -> str:
    """规范化**未链接**字节码：允许 40 字符库占位穿插在 hex 中。

    逐段扫描：普通段必须是偶数长度纯 hex；占位段必须恰好 40 字符且匹配
    新版/旧版占位正则。任何其他非 hex 字符（包括半个占位、破损占位）都拒绝，
    绝不把垃圾字符当作占位放过。
    """
    if value is None:
        raise BytecodeError("字节码为空")
    s = value.strip()
    if s.startswith("0x") or s.startswith("0X"):
        s = s[2:]
    if not s:
        raise BytecodeError("字节码为空")
    if len(s) % 2 != 0:
        raise BytecodeError(f"未链接字节码长度为奇数: {len(s)}")
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        j = i
        while j < n and s[j] in "0123456789abcdefABCDEF":
            j += 1
        seg = s[i:j]
        if len(seg) % 2 != 0:
            raise BytecodeError(f"未链接字节码中 hex 段长度为奇数（占位边界错位）@ {i}")
        out.append(seg.lower())
        i = j
        if i >= n:
            break
        # 非 hex 字符必须构成一个完整 40 字符占位
        token = s[i : i + 40]
        if len(token) != 40 or not (_PLACEHOLDER_NEW.fullmatch(token) or _PLACEHOLDER_LEGACY.fullmatch(token)):
            raise BytecodeError(f"未链接字节码含破损的库占位/非法字符 @ nibble {i}: {token[:40]!r}")
        out.append(token)
        i += 40
    return "".join(out)


def find_placeholders(nibbles: str) -> list[tuple[int, int, str]]:
    """返回所有未链接占位 ``(起始nibble, 结束nibble, 占位串)``，长均为 40。"""
    out: list[tuple[int, int, str]] = []
    for m in _PLACEHOLDER_NEW.finditer(nibbles):
        out.append((m.start(), m.end(), m.group(0)))
    for m in _PLACEHOLDER_LEGACY.finditer(nibbles):
        out.append((m.start(), m.end(), m.group(0)))
    out.sort()
    # 检测重叠（正则字符集互不相交，理论上不会重叠；仍然防御）
    for (s1, e1, _), (s2, e2, _) in zip(out, out[1:]):
        if s2 < e1:
            raise BytecodeError(f"库占位重叠 @ {s1}..{e1} / {s2}..{e2}")
    return out


def link_reference_regions(link_references: dict) -> list[tuple[str, str, int, int]]:
    """把 compiler output 的 linkReferences 展平。

    返回 ``(源路径, 库名, 起始nibble, 长度nibble)``，长度应为 40。
    """
    regions: list[tuple[str, str, int, int]] = []
    if not isinstance(link_references, dict):
        return regions
    for src_path, libs in link_references.items():
        if not isinstance(libs, dict):
            raise BytecodeError(f"linkReferences[{src_path}] 结构非法")
        for lib_name, occs in libs.items():
            if not isinstance(occs, list):
                raise BytecodeError(f"linkReferences 出现位置必须是列表: {src_path}:{lib_name}")
            for occ in occs:
                start = occ["start"]
                length = occ["length"]
                if not isinstance(start, int) or not isinstance(length, int) or start < 0 or length <= 0:
                    raise BytecodeError(f"非法 linkReference 区间: {src_path}:{lib_name} {occ}")
                regions.append((src_path, lib_name, start, length))
    regions.sort(key=lambda r: (r[2], r[3]))
    return regions


def mask_regions(nibbles_len: int, regions: list[tuple[int, int]]) -> bytearray:
    """构造掩码：1 表示忽略（库地址/元数据尾），0 表示必须逐 nibble 相同。"""
    mask = bytearray(nibbles_len)
    for start, length in regions:
        if start < 0 or length < 0 or start + length > nibbles_len:
            raise BytecodeError(f"掩码区间越界: start={start} length={length} total={nibbles_len}")
        for i in range(start, start + length):
            if mask[i]:
                raise BytecodeError(f"掩码区间重叠 @ nibble {i}")
            mask[i] = 1
    return mask


def diff_nibbles(expected: str, actual: str, mask: bytearray) -> list[dict]:
    """逐 nibble 比较；mask=1 的位置忽略。返回未被规则覆盖的差异。"""
    diffs: list[dict] = []
    i = 0
    n = len(expected)
    while i < n:
        if mask[i] == 0 and expected[i] != actual[i]:
            j = i + 1
            while j < n and mask[j] == 0 and expected[j] != actual[j]:
                j += 1
            diffs.append(
                {
                    "start_nibble": i,
                    "end_nibble": j,
                    "expected": expected[i:j],
                    "actual": actual[i:j],
                }
            )
            i = j
        else:
            i += 1
    return diffs
