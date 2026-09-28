"""Computation graph: node list, shape-checked construction, input layout.

A graph is an immutable topologically-ordered node list. Node ``i`` may only
consume nodes ``j < i``, so cycles are impossible by construction. ``input``
nodes declare the parameter layout; exactly one scalar node is the output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .errors import input_error
from .ops import OPS
from .tensor import Layout


@dataclass(frozen=True)
class Node:
    id: int
    op: str
    inputs: tuple[int, ...]
    params: dict[str, Any]
    shape: tuple[int, ...]
    name: str | None = None  # set for input nodes


class GraphBuilder:
    """Incremental, shape-checking node list builder.

    Also used by the autodiff module to extend an existing graph with
    gradient / HVP nodes; ``build_options`` carries per-request settings
    (e.g. the subgradient value) that backward rules may consult.
    """

    def __init__(
        self,
        nodes: Iterable[Node] | None = None,
        build_options: dict[str, Any] | None = None,
    ) -> None:
        self._nodes: list[Node] = list(nodes) if nodes else []
        self.build_options: dict[str, Any] = dict(build_options or {})

    @property
    def nodes(self) -> list[Node]:
        return self._nodes

    def shape_of(self, node_id: int) -> tuple[int, ...]:
        return self._nodes[node_id].shape

    def add_node(
        self,
        op: str,
        inputs: Iterable[int] = (),
        params: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> int:
        if op not in OPS:
            raise input_error("unknown op", op=op, known_ops=sorted(OPS))
        spec = OPS[op]
        inputs = tuple(int(i) for i in inputs)
        if len(inputs) != spec.arity:
            raise input_error(
                "op arity mismatch",
                op=op,
                expected_arity=spec.arity,
                actual_inputs=len(inputs),
            )
        for i in inputs:
            if not (0 <= i < len(self._nodes)):
                raise input_error(
                    "node inputs must refer to earlier nodes (no cycles)",
                    op=op,
                    input_id=i,
                    node_count=len(self._nodes),
                )
        params = dict(params or {})
        shape = spec.shape_fn([self._nodes[i].shape for i in inputs], params)
        node_id = len(self._nodes)
        self._nodes.append(
            Node(id=node_id, op=op, inputs=inputs, params=params, shape=shape, name=name)
        )
        return node_id

    def const(self, value: Any) -> int:
        return self.add_node("const", [], {"value": value})


@dataclass(frozen=True)
class Graph:
    nodes: tuple[Node, ...]
    layout: Layout
    output: int
    input_node_ids: tuple[int, ...]  # aligned with layout.slots order

    @property
    def node_count(self) -> int:
        return len(self.nodes)


def build_graph(spec_nodes: list[dict[str, Any]], output: int) -> Graph:
    """Build a validated graph from a client-supplied node spec list."""
    if not spec_nodes:
        raise input_error("graph must contain at least one node")
    builder = GraphBuilder()
    slot_decls: list[tuple[str, tuple[int, ...]]] = []
    input_node_ids: list[int] = []
    for idx, spec in enumerate(spec_nodes):
        if not isinstance(spec, dict):
            raise input_error("each node spec must be an object", index=idx)
        op = spec.get("op")
        inputs = spec.get("inputs", [])
        params = dict(spec.get("params") or {})
        if not isinstance(inputs, list):
            raise input_error("node 'inputs' must be a list of node ids", index=idx)
        name = None
        if op == "input":
            name = params.get("name")
            shape = tuple(params.get("shape") or ())
            slot_decls.append((name, shape))
        node_id = builder.add_node(op, inputs, params, name=name)
        if node_id != idx:
            raise input_error("node ids must be contiguous and in order", index=idx)
        if op == "input":
            input_node_ids.append(node_id)
    if not input_node_ids:
        raise input_error("graph must declare at least one 'input' node")
    if not (0 <= output < len(builder.nodes)):
        raise input_error("output node id out of range", output=output, node_count=len(builder.nodes))
    if builder.shape_of(output) != ():
        raise input_error(
            "output node must be scalar (shape []); reduce with 'sum' or 'dot'",
            output=output,
            shape=list(builder.shape_of(output)),
        )
    layout = Layout(slot_decls)
    return Graph(
        nodes=tuple(builder.nodes),
        layout=layout,
        output=output,
        input_node_ids=tuple(input_node_ids),
    )
