"""点分字段路径解析与匹配。

路径规则：
- ``user.id``        表示 dict 嵌套键
- ``orders[].price`` ``[]`` 是数组通配（只匹配列表），具体元素渲染为 ``[0]``、``[1]``
- ``%``、``.``、``[``、``]`` 在键中按百分号编码（``%xx``），
  因此记录里的字面键 ``"user.id"`` 编码后是 ``user%2eid``，
  不可能与嵌套路径 ``user.id`` 冲突 —— 嵌套结构无法通过起别名/怪键旁路策略。
"""

from __future__ import annotations

import re
from typing import List, Sequence

ARRAY = "[]"
# 编码后仍安全的字符：字母数字以及除 % . [ ] 外的常见标点
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_\- ]+$")
_SAFE_TOKEN = re.compile(r"^(?:\[\d+\])?$|^[^.\[\]]+$")
_INDEX_RE = re.compile(r"\[\d+\]")


class PathError(ValueError):
    """非法字段路径。"""


def encode_segment(seg: str) -> str:
    """把单个 dict 键编码为路径中的一个段。"""
    if not isinstance(seg, str):
        raise PathError(f"路径段必须是字符串，得到 {type(seg).__name__}")
    if _SAFE_SEGMENT.match(seg):
        return seg
    if seg == "":
        return "%00"
    out = []
    for ch in seg:
        if ch in ("%", ".", "[", "]"):
            out.append("%" + format(ord(ch), "02x"))
        else:
            out.append(ch)
    return "".join(out)


def decode_segment(token: str) -> str:
    """把路径段解码回原始 dict 键。"""
    if token == "%00":
        return ""
    out = []
    i = 0
    while i < len(token):
        ch = token[i]
        if ch == "%":
            if i + 2 >= len(token):
                raise PathError(f"百分号编码不完整: {token!r}")
            try:
                out.append(chr(int(token[i + 1 : i + 3], 16)))
            except ValueError as exc:
                raise PathError(f"非法百分号编码: {token!r}") from exc
            i += 3
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def split_path(path: str, *, allow_indices: bool = False) -> List[str]:
    """把点分路径拆成语义 token（键保持编码形式，数组为 ``[]``）。

    策略规则路径只允许 ``[]`` 通配；决策记录/输出中的实例路径含具体下标
    ``[0]``，调用方需显式传 ``allow_indices=True``。
    """
    if not isinstance(path, str) or path == "":
        raise PathError(f"路径必须是非空字符串，得到 {path!r}")
    tokens: List[str] = []
    i = 0
    n = len(path)
    while i < n:
        if path[i] == ".":
            # 前导/连续/尾随点均非法
            if i == 0 or i == n - 1 or path[i + 1] == ".":
                raise PathError(f"路径中点位置非法: {path!r}")
            i += 1
        if path[i] == "[":
            if path[i : i + 2] == "[]":
                tokens.append(ARRAY)
                i += 2
                if i < n and path[i] != ".":
                    raise PathError(f"'[]' 后只能是 '.' 或结尾: {path!r}")
            else:
                m = _INDEX_RE.match(path, i)
                if not m or not allow_indices:
                    raise PathError(
                        f"策略规则路径只允许 '[]' 通配，不允许具体下标: {path!r}"
                    )
                tokens.append(m.group(0))
                i = m.end()
                if i < n and path[i] != ".":
                    raise PathError(f"下标后只能是 '.' 或结尾: {path!r}")
        else:
            j = i
            while j < n and path[j] != ".":
                if path[j] == "[":
                    break
                if path[j] == "]":
                    raise PathError(f"路径含孤立 ']': {path!r}")
                j += 1
            token = path[i:j]
            if token == "":
                raise PathError(f"路径含空段: {path!r}")
            tokens.append(token)
            i = j
    for t in tokens:
        if t != ARRAY and not _SAFE_TOKEN.match(t):
            raise PathError(f"路径段含未编码保留字符: {path!r} (段={t!r})")
    return tokens


def join_tokens(tokens: Sequence[str]) -> str:
    """把 token 序列渲染成路径字符串。

    键前（除首个 token 外）总是需要点号；``[]``/``[k]`` 直接拼接::

        orders[].amount -> orders[].amount
        orders[2].items[0].sku
    """
    out = ""
    for idx, t in enumerate(tokens):
        if t == ARRAY or t.startswith("["):
            out += t
        else:
            if idx > 0:
                out += "."
            out += t
    return out


def instance_tokens(tokens: Sequence[str], indices: dict) -> List[str]:
    """把 ``[]`` 按当前栈下标渲染成 ``[k]``。"""
    depth = -1
    result: List[str] = []
    for t in tokens:
        if t == ARRAY:
            depth += 1
            result.append(f"[{indices.get(depth, 0)}]")
        else:
            result.append(t)
    return result


def prefix_of(ancestor: Sequence[str], descendant: Sequence[str]) -> bool:
    """``ancestor`` 是否为 ``descendant`` 的严格/非严格前缀。"""
    if len(ancestor) > len(descendant):
        return False
    return list(ancestor) == list(descendant[: len(ancestor)])


def shape_of_instance(shape_tokens: Sequence[str], instance_tokens_: Sequence[str]) -> bool:
    """具体实例路径是否对应某个形状路径（键相同、下标位对应 ``[]``）。"""
    if len(shape_tokens) != len(instance_tokens_):
        return False
    for a, b in zip(shape_tokens, instance_tokens_):
        if a == ARRAY:
            if not (b.startswith("[") and b.endswith("]") and b[1:-1].isdigit()):
                return False
        elif a != b:
            return False
    return True
