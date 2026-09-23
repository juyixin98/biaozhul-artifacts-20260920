"""AST 与分析结果的 JSON 序列化。IR 的序列化在 :mod:`constprop.model`。"""

from __future__ import annotations

from . import ast as A

_NODE_TYPES = {
    A.Program: "Program",
    A.Block: "Block",
    A.Assign: "Assign",
    A.Print: "Print",
    A.If: "If",
    A.While: "While",
    A.IntLit: "IntLit",
    A.BoolLit: "BoolLit",
    A.Var: "Var",
    A.Unary: "Unary",
    A.Binary: "Binary",
    A.Logical: "Logical",
}


def ast_to_dict(node) -> dict:
    def rec(n) -> dict | list | int | str | None:
        if isinstance(n, A.Node):
            d: dict = {"type": _NODE_TYPES[type(n)], "span": n.span.to_dict()}
            for field_name in n.__dataclass_fields__:
                if field_name == "span":
                    continue
                d[field_name] = rec(getattr(n, field_name))
            return d
        if isinstance(n, list):
            return [rec(x) for x in n]
        return n

    out = rec(node)
    assert isinstance(out, dict)
    return out
