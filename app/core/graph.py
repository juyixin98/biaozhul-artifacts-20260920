"""Computation graph: nodes, validation, topological order, reference counts.

A :class:`Graph` is a DAG of :class:`Node`.  Edges point from an input node to
the node that consumes it.  The same node may feed several consumers -- that
is a *shared subgraph*; its activation needs reference counting so it is kept
alive until its last consumer during both forward and replay segments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

from . import ops as ops_mod
from .errors import InvalidInputError

VALID_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


@dataclass(frozen=True)
class Node:
    id: str
    op: str
    inputs: Tuple[str, ...] = ()
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidatedGraph:
    nodes: Dict[str, Node]
    order: Tuple[str, ...]
    shapes: Dict[str, Tuple[int, ...]]
    """Topological order (inputs before consumers), deterministically chosen."""
    targets: Tuple[str, ...]
    """Designated output (loss) nodes."""
    consumers: Dict[str, Tuple[str, ...]]
    """node id -> ids of nodes that consume it, in topological order."""
    user_count: Dict[str, int]
    """How many non-root nodes consume each node (shared-subgraph counter)."""

    def is_shared(self, node_id: str) -> bool:
        return self.user_count.get(node_id, 0) > 1

    @property
    def target(self) -> str:
        if len(self.targets) != 1:
            raise InvalidInputError(
                "this graph requires exactly one target",
                code="E_GRAPH_TARGET_COUNT",
                context={"targets": list(self.targets)},
            )
        return self.targets[0]


def build_graph(nodes: Sequence[Node], targets: Sequence[str]) -> ValidatedGraph:
    """Validate raw nodes and produce an immutable, executable graph view."""

    if not nodes:
        raise InvalidInputError("graph has no nodes", code="E_GRAPH_EMPTY")
    by_id: Dict[str, Node] = {}
    for n in nodes:
        _validate_id(n.id)
        if n.id in by_id:
            raise InvalidInputError(
                f"duplicate node id {n.id!r}",
                code="E_GRAPH_DUP_ID",
                context={"id": n.id},
            )
        if n.op not in ops_mod.ALL_OPS:
            raise InvalidInputError(
                f"node {n.id!r} uses unknown op {n.op!r}",
                code="E_OP_UNKNOWN",
                context={"id": n.id, "op": n.op},
            )
        by_id[n.id] = Node(
            id=n.id,
            op=n.op,
            inputs=tuple(n.inputs),
            params=ops_mod.validate_params(n.op, n.params),
        )

    for n in by_id.values():
        spec = ops_mod.SPECS[n.op]
        lo, hi = spec.arity
        if not lo <= len(n.inputs) <= hi:
            raise InvalidInputError(
                f"node {n.id!r} ({n.op}) expects {lo}..{hi} inputs, got "
                f"{len(n.inputs)}",
                code="E_OP_ARITY",
                context={"id": n.id, "op": n.op, "got": len(n.inputs)},
            )
        for ref in n.inputs:
            if ref not in by_id:
                raise InvalidInputError(
                    f"node {n.id!r} references missing input {ref!r}",
                    code="E_GRAPH_MISSING_REF",
                    context={"id": n.id, "missing": ref},
                )
            if ref == n.id:
                raise InvalidInputError(
                    f"node {n.id!r} references itself",
                    code="E_GRAPH_SELF_REF",
                    context={"id": n.id},
                )

    order = _topological_order(by_id)
    shapes = _infer_shapes(by_id, order)
    consumers, user_count = _build_consumers(by_id, order)

    if not targets:
        raise InvalidInputError(
            "graph requires at least one target", code="E_GRAPH_NO_TARGET"
        )
    for t in targets:
        if t not in by_id:
            raise InvalidInputError(
                f"target {t!r} does not exist",
                code="E_GRAPH_MISSING_TARGET",
                context={"target": t},
            )
        # A node consumed by another node cannot be the loss target.
        if user_count.get(t, 0) != 0:
            raise InvalidInputError(
                f"target {t!r} is consumed by another node",
                code="E_GRAPH_TARGET_NOT_FINAL",
                context={"target": t, "consumers": list(consumers[t])},
            )

    return ValidatedGraph(
        nodes=by_id,
        order=tuple(order),
        shapes=shapes,
        targets=tuple(targets),
        consumers=consumers,
        user_count=user_count,
    )


def _validate_id(node_id: str) -> None:
    if not isinstance(node_id, str) or not node_id:
        raise InvalidInputError(
            "node id must be a non-empty string",
            code="E_GRAPH_BAD_ID",
            context={"id": node_id},
        )
    if len(node_id) > 64 or any(c not in VALID_ID_CHARS for c in node_id):
        raise InvalidInputError(
            "node id may only contain letters, digits, '_' and '-' (max 64)",
            code="E_GRAPH_BAD_ID",
            context={"id": node_id},
        )


def _topological_order(by_id: Dict[str, Node]) -> List[str]:
    """Kahn's algorithm; ties broken by id for a deterministic order."""

    indeg = {nid: len(n.inputs) for nid, n in by_id.items()}
    dependents: Dict[str, List[str]] = {nid: [] for nid in by_id}
    for nid, n in by_id.items():
        for ref in n.inputs:
            dependents[ref].append(nid)
    ready = sorted(nid for nid, d in indeg.items() if d == 0)
    order: List[str] = []
    while ready:
        cur = ready.pop(0)
        order.append(cur)
        for dep in dependents[cur]:
            indeg[dep] -= 1
            if indeg[dep] == 0:
                ready.append(dep)
                ready.sort()
    if len(order) != len(by_id):
        cyclic = [nid for nid, d in indeg.items() if d > 0]
        raise InvalidInputError(
            "graph contains a cycle",
            code="E_GRAPH_CYCLE",
            context={"nodes_in_cycle": sorted(cyclic)},
        )
    return order


def _infer_shapes(by_id: Dict[str, Node], order: Sequence[str]
                  ) -> Dict[str, Tuple[int, ...]]:
    shapes: Dict[str, Tuple[int, ...]] = {}
    for nid in order:
        n = by_id[nid]
        in_shapes = [shapes[r] for r in n.inputs]
        try:
            shapes[nid] = ops_mod.infer_shape(n.op, n.params, in_shapes)
        except InvalidInputError as exc:
            exc.context["node_id"] = nid
            raise
    return shapes


def _build_consumers(by_id: Dict[str, Node], order: Sequence[str]
                     ) -> Tuple[Dict[str, Tuple[str, ...]], Dict[str, int]]:
    consumers: Dict[str, List[str]] = {nid: [] for nid in by_id}
    user_count: Dict[str, int] = {nid: 0 for nid in by_id}
    rank = {nid: i for i, nid in enumerate(order)}
    for n in by_id.values():
        for ref in n.inputs:
            consumers[ref].append(n.id)
            user_count[ref] += 1
    for nid in consumers:
        consumers[nid].sort(key=lambda x: rank[x])
    return {k: tuple(v) for k, v in consumers.items()}, user_count
