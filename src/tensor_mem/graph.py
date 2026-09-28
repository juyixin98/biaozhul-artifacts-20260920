"""Computation graph: nodes, validated construction, topology and waves.

A graph is a DAG of :class:`Node` objects. Feeds are external inputs identified
by name. Construction validates arity, unknown references, dtype agreement and
runs shape inference eagerly, so downstream modules work against resolved
:class:`~tensor_mem.tensor.TensorMeta`.

"Waves" partition nodes so that every node in a wave depends only on strictly
earlier waves; nodes in the same wave are mutually independent and may run
concurrently -- therefore their *live* buffers must never share storage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import GraphValidationError, InputValidationError
from .ops import OpSpec, get_op
from .tensor import Shape, TensorMeta, TensorType, shape_from


@dataclass(frozen=True)
class Node:
    """One op invocation."""

    id: str
    op: str
    inputs: tuple[str, ...]   # feed names or upstream node ids
    attrs: tuple[tuple[str, Any], ...]
    outputs: tuple[str, ...]  # named outputs (one per op output slot)
    spec: OpSpec = field(repr=False, compare=False)
    out_meta: tuple[TensorMeta, ...] = field(repr=False, compare=False)
    in_meta: tuple[TensorMeta, ...] = field(repr=False, compare=False)

    @property
    def attrs_dict(self) -> dict[str, Any]:
        return dict(self.attrs)


@dataclass
class Graph:
    nodes: dict[str, Node]
    # output name -> producing node id (None means it is a feed)
    producer: dict[str, str | None]
    feeds: dict[str, TensorMeta]
    graph_outputs: tuple[str, ...]
    order: list[str]  # topological node ids
    waves: list[list[str]]

    def node(self, node_id: str) -> Node:
        try:
            return self.nodes[node_id]
        except KeyError:
            raise GraphValidationError("unknown node", node=node_id) from None

    @property
    def node_count(self) -> int:
        return len(self.nodes)


class GraphBuilder:
    """Accumulates declared feeds/nodes and produces a validated :class:`Graph`."""

    def __init__(self) -> None:
        self._feeds: dict[str, TensorMeta] = {}
        self._nodes: dict[str, Node] = {}
        self._producer: dict[str, str | None] = {}
        self._declared_outputs: list[str] = []

    def feed(self, name: str, dtype: str, shape: Any) -> "GraphBuilder":
        self._check_fresh_name(name)
        s = shape_from(shape)
        meta = TensorMeta(TensorType(dtype, s.rank), s)
        self._feeds[name] = meta
        self._producer[name] = None
        return self

    def node(
        self,
        node_id: str,
        op: str,
        inputs: list[str] | tuple[str, ...],
        outputs: list[str] | tuple[str, ...],
        attrs: dict[str, Any] | None = None,
    ) -> "GraphBuilder":
        if not isinstance(node_id, str) or not node_id:
            raise InputValidationError("node id must be a non-empty string")
        self._check_fresh_name(node_id)
        spec = get_op(op)
        if len(inputs) != spec.ninputs:
            raise GraphValidationError(
                f"op {op!r} expects {spec.ninputs} inputs, got {len(inputs)}",
                op=op, expected=spec.ninputs, got=len(inputs),
            )
        if len(outputs) != spec.noutputs:
            raise GraphValidationError(
                f"op {op!r} expects {spec.noutputs} outputs, got {len(outputs)}",
                op=op, expected=spec.noutputs, got=len(outputs),
            )
        for out in outputs:
            self._check_fresh_name(out)

        in_meta: list[TensorMeta] = []
        for ref in inputs:
            if not isinstance(ref, str) or not ref:
                raise InputValidationError("input references must be non-empty strings")
            if ref not in self._producer:
                raise GraphValidationError(
                    "node input references an unknown tensor",
                    node=node_id, input=ref,
                )
            producer = self._producer[ref]
            if producer is None:
                in_meta.append(self._feeds[ref])
            else:
                prod_node = self._nodes[producer]
                slot = prod_node.outputs.index(ref)
                in_meta.append(prod_node.out_meta[slot])

        attrs = dict(attrs or {})
        try:
            out_types, out_shapes = spec.infer(
                [m.tensor_type for m in in_meta],
                [m.shape for m in in_meta],
                attrs,
            )
        except (GraphValidationError, InputValidationError):
            raise
        out_meta = tuple(
            TensorMeta(t, s) for t, s in zip(out_types, out_shapes)
        )

        node = Node(
            id=node_id, op=op, inputs=tuple(inputs),
            attrs=tuple(sorted(attrs.items())),
            outputs=tuple(outputs), spec=spec,
            out_meta=out_meta, in_meta=tuple(in_meta),
        )
        self._nodes[node_id] = node
        for out in outputs:
            self._producer[out] = node_id
        return self

    def graph_outputs(self, names: list[str] | tuple[str, ...]) -> "GraphBuilder":
        for name in names:
            if name not in self._producer:
                raise GraphValidationError(
                    "declared graph output is unknown", output=name
                )
        self._declared_outputs = list(names)
        return self

    def build(self) -> Graph:
        if not self._nodes:
            raise GraphValidationError("graph must contain at least one node")
        if not self._declared_outputs:
            raise GraphValidationError("graph must declare at least one output")
        order = self._topological_sort()
        waves = self._compute_waves(order)
        return Graph(
            nodes=dict(self._nodes),
            producer=dict(self._producer),
            feeds=dict(self._feeds),
            graph_outputs=tuple(self._declared_outputs),
            order=order,
            waves=waves,
        )

    # ------------------------------------------------------------------ #

    def _check_fresh_name(self, name: str) -> None:
        if not isinstance(name, str) or not name:
            raise InputValidationError("tensor/node name must be a non-empty string")
        if name in self._producer or name in self._nodes:
            raise GraphValidationError("name already defined", name=name)

    def _topological_sort(self) -> list[str]:
        # Kahn's algorithm; also rejects cycles.
        indegree: dict[str, int] = {nid: 0 for nid in self._nodes}
        children: dict[str, list[str]] = {nid: [] for nid in self._nodes}
        for nid, node in self._nodes.items():
            for ref in node.inputs:
                producer = self._producer[ref]
                if producer is not None:  # feeds have no node dependency
                    indegree[nid] += 1
                    children[producer].append(nid)

        ready = sorted(nid for nid, d in indegree.items() if d == 0)
        order: list[str] = []
        while ready:
            nid = ready.pop(0)
            order.append(nid)
            for child in children[nid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    # keep deterministic ordering for replay logs
                    ready.append(child)
                    ready.sort()
        if len(order) != len(self._nodes):
            cyclic = [n for n, d in indegree.items() if d > 0]
            raise GraphValidationError("graph contains a cycle", nodes=cyclic)
        return order

    def _compute_waves(self, order: list[str]) -> list[list[str]]:
        """ASAP wave number: 1 + max wave of node dependencies.

        Nodes sharing a wave have no dependency path between them and are the
        concurrency set the planner must keep disjoint.
        """
        wave_of: dict[str, int] = {}
        for nid in order:
            node = self._nodes[nid]
            dep_waves = [
                wave_of[self._producer[ref]]
                for ref in node.inputs
                if self._producer[ref] is not None
            ]
            wave_of[nid] = (max(dep_waves) + 1) if dep_waves else 0
        max_wave = max(wave_of.values())
        waves: list[list[str]] = [[] for _ in range(max_wave + 1)]
        for nid, w in wave_of.items():
            waves[w].append(nid)
        for w in waves:
            w.sort()
        return waves
