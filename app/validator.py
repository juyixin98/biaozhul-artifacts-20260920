"""Structural validation of a process definition, executed before publish.

Rejects:
* duplicate node ids, unknown edge references, duplicate parallel edges
* missing/misplaced fields per node type (start/approval/condition/end)
* wrong start/end cardinalities, dangling nodes, dead ends
* unreachable nodes and directed cycles (including self loops)
* condition nodes without an expression-less default edge
* syntactically invalid restricted expressions (never executed, only compiled)
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from .expression import ExpressionError, compile_expression
from .schemas import Definition


def validate_definition(definition: Definition) -> list[dict[str, str]]:
    """Return a list of issues; an empty list means the definition is valid."""
    issues: list[dict[str, str]] = []

    def add(code: str, message: str, node_id: str | None = None) -> None:
        issue = {"code": code, "message": message}
        if node_id is not None:
            issue["node_id"] = node_id
        issues.append(issue)

    nodes = definition.nodes
    edges = definition.edges

    # -- duplicate node ids --------------------------------------------------
    by_id: dict[str, Any] = {}
    for node in nodes:
        if node.id in by_id:
            add("duplicate_node", f"duplicate node id {node.id!r}", node.id)
        by_id[node.id] = node

    starts = [n for n in nodes if n.type == "start"]
    ends = [n for n in nodes if n.type == "end"]
    approvals = [n for n in nodes if n.type == "approval"]
    conditions = [n for n in nodes if n.type == "condition"]

    if len(starts) != 1:
        add("start_cardinality", f"expected exactly 1 start node, found {len(starts)}")
    if not ends:
        add("end_cardinality", "at least one end node is required")

    # -- per-node field rules ------------------------------------------------
    for node in nodes:
        if node.type == "approval":
            if not node.mode:
                add("approval_mode_required", "approval node requires mode 'all' or 'any'", node.id)
            if not node.assignees or any(not a for a in node.assignees):
                add("approval_assignees_required", "approval node requires a non-empty assignees list", node.id)
            if (node.timeout_seconds is None) != (not node.escalate_to):
                add(
                    "escalation_config",
                    "timeout_seconds and escalate_to must be set together",
                    node.id,
                )
            if node.terminal is not None:
                add("unexpected_field", "terminal is only valid on end nodes", node.id)
        elif node.type == "condition":
            if node.assignees or node.mode or node.timeout_seconds or node.terminal:
                add("unexpected_field", "condition node only carries id/type/name", node.id)
        elif node.type == "end":
            if node.terminal not in ("approved", "rejected"):
                add("end_terminal_required", "end node requires terminal 'approved' or 'rejected'", node.id)
        elif node.type == "start":
            if node.assignees or node.mode or node.terminal:
                add("unexpected_field", "start node only carries id/type/name", node.id)

    # -- edges ---------------------------------------------------------------
    outgoing: dict[str, list[Any]] = defaultdict(list)
    incoming: dict[str, list[Any]] = defaultdict(list)
    seen_pairs: set[tuple[str, str]] = set()
    for edge in edges:
        if edge.source not in by_id:
            add("unknown_edge_source", f"edge references unknown source {edge.source!r}", edge.source)
            continue
        if edge.target not in by_id:
            add("unknown_edge_target", f"edge references unknown target {edge.target!r}", edge.target)
            continue
        pair = (edge.source, edge.target)
        if pair in seen_pairs:
            add("duplicate_edge", f"duplicate edge {edge.source!r} -> {edge.target!r}", edge.source)
        seen_pairs.add(pair)
        outgoing[edge.source].append(edge)
        incoming[edge.target].append(edge)

    # edge expression rules
    for node_id, outs in outgoing.items():
        if node_id not in by_id:
            continue
        node = by_id[node_id]
        if node.type == "condition":
            if len(outs) < 2:
                add("condition_branches", "condition node needs at least two outgoing edges", node_id)
            default_edges = [e for e in outs if e.expression is None]
            if len(default_edges) != 1:
                add("condition_default", "condition node needs exactly one default edge (no expression)", node_id)
            expressions = [e.expression for e in outs if e.expression is not None]
            if len(expressions) != len(set(expressions)):
                add("condition_duplicate_expression", "duplicate expressions on condition branches", node_id)
            for expr in expressions:
                try:
                    compile_expression(expr)
                except ExpressionError as exc:
                    add("invalid_expression", f"invalid expression {expr!r}: {exc}", node_id)
        else:
            bad = [e for e in outs if e.expression is not None]
            if bad:
                add("unexpected_expression", f"only condition nodes may branch on expressions", node_id)

    if not starts:
        return issues  # the graph checks below need a start

    # -- type-based out-degree rules ----------------------------------------
    for node in nodes:
        if node.type == "start" and len(outgoing[node.id]) != 1:
            add("start_outdegree", "start node must have exactly one outgoing edge", node.id)
        if node.type == "approval" and len(outgoing[node.id]) != 1:
            add("approval_outdegree", "approval node must have exactly one outgoing edge", node.id)
        if node.type == "end" and outgoing[node.id]:
            add("end_outdegree", "end node must have no outgoing edges", node.id)

    # -- reachability from start --------------------------------------------
    reachable: set[str] = set()
    queue = deque([starts[0].id])
    while queue:
        cur = queue.popleft()
        if cur in reachable:
            continue
        reachable.add(cur)
        for edge in outgoing.get(cur, []):
            if edge.target in by_id and edge.target not in reachable:
                queue.append(edge.target)
    for node in nodes:
        if node.id not in reachable:
            add("unreachable_node", f"node {node.id!r} is not reachable from start", node.id)

    # -- end reachability / dead ends (over the reachable subgraph) ----------
    for node in nodes:
        if node.id not in reachable:
            continue
        if node.type != "end" and not outgoing[node.id]:
            add("dead_end", f"non-end node {node.id!r} has no outgoing edge", node.id)
        if node.id != starts[0].id and not incoming[node.id] and node.type != "end":
            # end nodes with no incoming path were already caught as unreachable
            add("orphan_node", f"node {node.id!r} has no incoming edge", node.id)

    # -- cycle detection (DFS over the reachable graph) ----------------------
    state: dict[str, int] = {}  # 0 = unvisited, 1 = on stack, 2 = done

    def dfs(node_id: str) -> bool:
        state[node_id] = 1
        for edge in outgoing.get(node_id, []):
            target = edge.target
            if target not in by_id:
                continue
            if state.get(target, 0) == 1:
                return True
            if state.get(target, 0) == 0 and dfs(target):
                return True
        state[node_id] = 2
        return False

    for node_id in reachable:
        if state.get(node_id, 0) == 0 and dfs(node_id):
            add("cycle_detected", "the process graph contains a cycle")
            break

    return issues
