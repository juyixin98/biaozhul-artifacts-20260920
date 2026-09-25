"""Interprocedural terminal summaries.

A path-sensitive analysis of one function needs to know, for each call, which
of the call's two outcomes can actually happen:

* normal completion (the callee returns)
* exceptional completion (only possible when the callee is declared ``throws``)

Treating every ``throws`` callee as "may return normally / may throw" is a
sound over-approximation but creates impossible paths for functions whose
whole body always throws, or whose declared ``throws`` can never happen. We
therefore compute precise summaries as simultaneous least fixed points on the
call graph:

* ``may_return[f]``: the synthetic normal exit is reachable in f's CFG
* ``may_throw[f]``: the synthetic exception exit is reachable

A call's normal edge is usable exactly when ``may_return[callee]``; its
exception edge (only emitted for callees declared ``throws``) is usable exactly
when ``may_throw[callee]``. Recursive SCCs are handled naturally: least fixed
points keep outcomes false unless a route exists that does not circularly
depend on the same outcome.
"""
from __future__ import annotations

from typing import Dict, List, Set, Tuple

from . import ast_nodes as ast
from .cfg import EXCEPTION, NORMAL, FunctionCfg


def compute_summaries(
    cfgs: Dict[str, FunctionCfg],
    funcs: Dict[str, ast.Function],
) -> Tuple[Dict[str, bool], Dict[str, bool]]:
    names = list(cfgs.keys())
    may_return: Dict[str, bool] = {name: False for name in names}
    may_throw: Dict[str, bool] = {name: False for name in names}

    def reachable(cfg: FunctionCfg, target: str) -> bool:
        other = cfg.except_exit if target == cfg.exit else cfg.exit
        adjacency: Dict[str, List] = {}
        for edge in cfg.edges:
            adjacency.setdefault(edge.src, []).append(edge)
        seen: Set[str] = set()
        stack: List[str] = [cfg.entry]
        while stack:
            node_id = stack.pop()
            if node_id == target:
                return True
            if node_id in seen or node_id == other:
                continue
            seen.add(node_id)
            node = cfg.nodes[node_id]
            for edge in adjacency.get(node_id, []):
                usable = True
                if node.kind == "call":
                    callee = node.call.name
                    if edge.kind == NORMAL:
                        usable = may_return[callee]
                    elif edge.kind == EXCEPTION:
                        usable = may_throw[callee]
                if usable and edge.dst not in seen:
                    stack.append(edge.dst)
        return False

    changed = True
    while changed:
        changed = False
        for name in names:
            cfg = cfgs[name]
            if not may_return[name] and reachable(cfg, cfg.exit):
                may_return[name] = True
                changed = True
            if not may_throw[name] and reachable(cfg, cfg.except_exit):
                may_throw[name] = True
                changed = True
    return may_return, may_throw
