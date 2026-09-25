"""JSON 路径解析与匹配。

支持的路径语法（段之间用 ``.`` 分隔，整体可带可选 ``$`` 前缀）：

* ``foo``          —— 键名为 foo 的对象字段
* ``foo.bar``      —— 嵌套字段
* ``*``            —— 段通配：当前对象/数组的所有子级
* ``["foo bar"]``  —— 方括号键名（支持带点、空格、星号的字面量键）
* ``['foo']``      —— 同上，单引号
* ``[0]``          —— 数组下标
* ``[*]``          —— 等同 ``*``
* ``..``           —— 递归下降：其后的一个段在任意深度匹配
* ``$``            —— 可选的根前缀

匹配结果为"踪迹"（trail）：``tuple[str | int, ...]``，空元组表示文档根。

本模块只处理路径结构，不接触字段值，因此路径解析错误不可能泄露敏感数据。
"""

from __future__ import annotations

from typing import Any, Iterator, Union

Segment = Union[str, int, None]  # None 表示递归下降标记；"*" 表示通配段
Trail = tuple[Union[str, int], ...]

_RECURSIVE = object()  # 内部占位符：递归下降标记
_WILDCARD = "*"
_ALLOWED_BARE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def parse_path(raw: Any) -> tuple[Any, ...]:
    """把路径字符串解析为段元组。

    抛出 ``ValueError``（由编译层包装为 RuleCompileError）。返回的段中：

    * ``_RECURSIVE`` 表示递归下降
    * ``"*"`` 表示通配
    * ``int`` 表示数组下标
    * 其余 ``str`` 为字面量键名
    """
    if not isinstance(raw, str):
        raise ValueError("path 必须是字符串")
    s = raw.strip()
    if not s:
        raise ValueError("path 不能为空")
    if s.startswith("$"):
        s = s[1:]
        if s and not s.startswith((".", "[")):
            raise ValueError(f"根标记 $ 后只能跟 . 或 [：{_quote(raw)}")
    s = s.strip()
    if not s:
        return ()  # 仅 "$"：匹配文档根
    if s == ".." or s == ".":
        raise ValueError("递归下降 .. 之后必须跟一个段")
    if s.startswith(".") and not s.startswith(".."):
        s = s[1:].strip()
        if not s:
            raise ValueError("点之后必须跟一个段")

    segs: list[Any] = []
    i = 0
    n = len(s)
    expect_segment = True  # 起始或分隔符之后必须出现一个段
    while i < n:
        if s.startswith("..", i):
            # foo..bar 合法（成员后的递归下降）；. .foo 非法
            if expect_segment and i > 0:
                raise ValueError(f"递归下降 .. 位置非法（{i}）：{_quote(raw)}")
            segs.append(_RECURSIVE)
            i += 2
            if i >= n:
                raise ValueError("递归下降 .. 之后必须跟一个段")
            expect_segment = True
            continue
        if s[i] == ".":
            if expect_segment:
                raise ValueError(f"出现多余的点（位置 {i}）：{_quote(raw)}")
            i += 1
            expect_segment = True
            continue
        if s[i] == "[":
            # bare key 后可直接跟 [（如 users[*]），其余情况下必须处于段起始
            if expect_segment is False and (i == 0 or s[i - 1] == "]" or s[i - 1] in _ALLOWED_BARE):
                pass  # 合法的相邻段：users[*]、items[2][*]
            elif not expect_segment:
                raise ValueError(f"括号段前缺少分隔（位置 {i}）：{_quote(raw)}")
            seg, i = _parse_bracket(s, i)
            segs.append(seg)
            expect_segment = False
            continue
        # bare key
        if not expect_segment:
            raise ValueError(f"路径段之间缺少点号（位置 {i}）：{_quote(raw)}")
        j = i
        while j < n and s[j] in _ALLOWED_BARE:
            j += 1
        if j == i:
            raise ValueError(f"无法解析路径段（位置 {i}）：{_quote(raw)}")
        segs.append(s[i:j])
        i = j
        expect_segment = False
    if expect_segment:
        raise ValueError(f"路径以分隔符结尾，缺少路径段：{_quote(raw)}")
    if not segs:
        raise ValueError(f"路径未包含任何段：{_quote(raw)}")
    return tuple(segs)


def _parse_bracket(s: str, i: int) -> tuple[Any, int]:
    assert s[i] == "["
    j = s.find("]", i + 1)
    if j == -1:
        raise ValueError("方括号未闭合")
    inner = s[i + 1 : j]
    k = j + 1
    if k < len(s) and s[k] not in (".", "["):
        raise ValueError(f"] 后只能跟 . 或 [（位置 {k}）")
    if inner == "*":
        return _WILDCARD, k
    if len(inner) >= 2 and inner[0] in "\"'" and inner[-1] == inner[0]:
        key = inner[1:-1]
        if not key:
            raise ValueError("方括号键名不能为空")
        return key, k
    if inner and (inner.isdigit() or (inner[0] == "-" and inner[1:].isdigit())):
        idx = int(inner)
        if idx < 0:
            raise ValueError("数组下标不支持负数")
        return idx, k
    raise ValueError(f"方括号内容必须是 *、引号字符串或非负整数：{_quote(inner)}")


def _quote(v: str) -> str:
    """路径错误展示用：路径来自规则而非数据，属 schema 信息，可安全回显。"""
    return repr(v)


def canonical(raw: str) -> str:
    """路径的规范化形式，用于编译期发现重复模式。"""
    return format_segments(parse_path(raw))


def format_segments(segs: tuple[Any, ...]) -> str:
    """把段元组渲染为无歧义的规范化路径串。"""
    out = ["$"]
    for seg in segs:
        if seg is _RECURSIVE:
            out.append("..")
        elif seg == _WILDCARD:
            out.append("[*]")
        elif isinstance(seg, int):
            out.append(f"[{seg}]")
        elif isinstance(seg, str) and all(c in _ALLOWED_BARE for c in seg):
            out.append(seg if out[-1].endswith(".") else "." + seg)
        elif isinstance(seg, str):
            out.append("[" + _json_string(seg) + "]")
    return "".join(out)


def _json_string(s: str) -> str:
    # 规则键名来自配置，使用简单转义即可
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def find_matches(doc: Any, segments: tuple[Any, ...]) -> list[Trail]:
    """返回所有匹配踪迹（按文档遍历顺序、去重）。"""
    results: list[Trail] = []
    seen: set[Trail] = set()

    def emit(trail: Trail) -> None:
        if trail not in seen:
            seen.add(trail)
            results.append(trail)

    def walk(node: Any, trail: Trail, segs: tuple[Any, ...]) -> None:
        if not segs:
            emit(trail)
            return
        head, rest = segs[0], segs[1:]
        if head is _RECURSIVE:
            if not rest:
                return
            # 当前层级先尝试匹配后续段
            walk(node, trail, rest)
            # 再下降到每个子级
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, trail + (k,), segs)
            elif isinstance(node, list):
                for idx, v in enumerate(node):
                    walk(v, trail + (idx,), segs)
            return
        _match_step(node, trail, head, rest, walk)

    walk(doc, (), segments)
    return results


def _match_step(node: Any, trail: Trail, head: Any, rest: tuple[Any, ...], walk) -> None:
    if head == _WILDCARD:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, trail + (k,), rest)
        elif isinstance(node, list):
            for idx, v in enumerate(node):
                walk(v, trail + (idx,), rest)
        return
    if isinstance(head, int):
        if isinstance(node, list) and 0 <= head < len(node):
            walk(node[head], trail + (head,), rest)
        return
    # 字面量键
    if isinstance(node, dict) and head in node:
        walk(node[head], trail + (head,), rest)


def get_at(doc: Any, trail: Trail) -> Any:
    node = doc
    for part in trail:
        node = node[part]
    return node


def set_at(doc: Any, trail: Trail, value: Any) -> Any:
    """就地替换 trail 处的值；trail 为空时返回新的根。"""
    if not trail:
        return value
    parent = get_at(doc, trail[:-1])
    parent[trail[-1]] = value
    return doc


def iter_trails(doc: Any) -> Iterator[Trail]:
    """遍历文档中所有字段位置（含根），供引擎收集规则匹配。"""
    yield ()
    if isinstance(doc, dict):
        for k, v in doc.items():
            for sub in _iter_from(v, (k,)):
                yield sub
    elif isinstance(doc, list):
        for idx, v in enumerate(doc):
            for sub in _iter_from(v, (idx,)):
                yield sub


def _iter_from(node: Any, trail: Trail) -> Iterator[Trail]:
    yield trail
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _iter_from(v, trail + (k,))
    elif isinstance(node, list):
        for idx, v in enumerate(node):
            yield from _iter_from(v, trail + (idx,))
