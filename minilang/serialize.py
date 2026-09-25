"""JSON serialisation for parse results."""
from __future__ import annotations

from typing import Any, List, Optional

from .document import ParseResult
from .lexer import Diagnostic, offset_to_line_col
from .nodes import Node


def _span_dict(start: int, end: int, line_starts: Optional[List[int]]) -> dict:
    span: dict = {"start": start, "end": end}
    if line_starts is not None:
        sl, sc = offset_to_line_col(line_starts, start)
        el, ec = offset_to_line_col(line_starts, end)
        span["start_line"] = sl
        span["start_col"] = sc
        span["end_line"] = el
        span["end_col"] = ec
    return span


def _value_to_json(value: Any, line_starts: Optional[List[int]]) -> Any:
    if isinstance(value, dict):
        if set(value.keys()) == {"start", "end"} and all(
            isinstance(v, int) for v in value.values()
        ):
            return _span_dict(value["start"], value["end"], line_starts)
        return {k: _value_to_json(v, line_starts) for k, v in value.items()}
    if isinstance(value, list):
        return [_value_to_json(v, line_starts) for v in value]
    return value


def node_to_dict(node: Node, line_starts: Optional[List[int]] = None) -> dict:
    return {
        "kind": node.kind,
        "span": _span_dict(node.start, node.end, line_starts),
        "value": _value_to_json(node.value, line_starts),
        "children": [node_to_dict(c, line_starts) for c in node.children],
    }


def diagnostic_to_dict(
    diagnostic: Diagnostic, line_starts: Optional[List[int]] = None
) -> dict:
    return diagnostic.to_dict(line_starts)


def result_to_dict(result: ParseResult, with_positions: bool = True) -> dict:
    lines = result.line_starts if with_positions else None
    return {
        "tree": node_to_dict(result.tree, lines),
        "diagnostics": [
            diagnostic_to_dict(d, lines) for d in result.diagnostics
        ],
        "stats": result.stats,
    }
