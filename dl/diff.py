"""规范化树、差分比较与 JSON 序列化。

差分测试以全量解析为标尺，与增量结果比较两样东西：

1. **规范化树** ``canonical()``：含 kind、字符范围、令牌范围、text
   与子树；不含 node_id（复用标识）、pkey（内部例程标签），
   错误也不进树（单独比较）。
2. **错误列表** ``(message, start, end)`` 三元组（两边均按位置排序）。
"""

from __future__ import annotations

from typing import Any

from .nodes import Node, ParseError
from .parser import ParseResult


def canonical(node: Node | None) -> Any:
    """可直接用 == 比较的规范化树表示。"""
    if node is None:
        return None
    return [
        node.kind,
        node.start,
        node.end,
        node.tok_start,
        node.tok_end,
        node.text,
        [canonical(c) for c in node.children],
    ]


def canonical_errors(errors: list[ParseError]) -> list[tuple[str, int, int]]:
    return sorted(
        (e.message, e.start, e.end) for e in errors
    )


def assert_same(full: ParseResult, incr: ParseResult) -> None:
    """断言一次增量解析结果与全量解析一致；失败时给出定位信息。"""
    ct_full = canonical(full.tree)
    ct_incr = canonical(incr.tree)
    if ct_full != ct_incr:
        path = _first_diff(ct_full, ct_incr, [])
        raise AssertionError(
            "增量树与全量树不一致，首个差异路径："
            + ".".join(map(str, path or ["?"]))
            + f"\n  全量: {_at(ct_full, path)!r}"
            + f"\n  增量: {_at(ct_incr, path)!r}"
        )
    er_full = canonical_errors(full.errors)
    er_incr = canonical_errors(incr.errors)
    if er_full != er_incr:
        raise AssertionError(
            f"错误列表不一致\n  全量: {er_full}\n  增量: {er_incr}"
        )


def _first_diff(a, b, path: list):
    if type(a) is not type(b):
        return path
    if isinstance(a, list):
        if len(a) != len(b):
            return path + ["len"]
        for i, (x, y) in enumerate(zip(a, b)):
            d = _first_diff(x, y, path + [i])
            if d is not None:
                return d
        return None
    return None if a == b else path


def _at(tree, path):
    cur = tree
    for p in path or []:
        if isinstance(cur, list) and isinstance(p, int) and p < len(cur):
            cur = cur[p]
        else:
            return cur
    return cur


# ---------- JSON 序列化 ----------

def node_to_dict(node: Node, *, include_ids: bool = True) -> dict[str, Any]:
    d: dict[str, Any] = {
        "kind": node.kind,
        "start": node.start,
        "end": node.end,
        "tok_start": node.tok_start,
        "tok_end": node.tok_end,
        "text": node.text,
        "children": [node_to_dict(c, include_ids=include_ids)
                     for c in node.children],
    }
    if include_ids:
        d["node_id"] = node.node_id
    return d


def result_to_dict(result: ParseResult, *, include_tokens: bool = False,
                   include_ids: bool = True) -> dict[str, Any]:
    d: dict[str, Any] = {
        "tree": node_to_dict(result.tree, include_ids=include_ids),
        "errors": [e.to_dict() for e in result.errors],
    }
    if include_tokens:
        d["tokens"] = [t.to_dict() for t in result.tokens]
    return d
