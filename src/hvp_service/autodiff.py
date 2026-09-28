"""Double-backward autodiff: gradient and Hessian-vector product programs.

HVP is computed as ``∇(x ↦ ⟨∇f(x), v⟩)`` — reverse mode over the gradient
graph ("reverse-over-reverse"). The gradient graph is built with the same
operator set as the forward graph, so the second reverse pass reuses the
same backward rules. The full Hessian is never materialized: memory and
compute scale with the graph size, not with ``n²``.

Shared subgraphs are handled by adjoint *accumulation*: when several
consumers contribute to the same node, their adjoints are combined with
explicit ``add`` nodes, so every shared subexpression is evaluated once in
the forward pass and its contributions are summed exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .graph import Graph, GraphBuilder, Node
from .ops import OPS, BackwardCtx


def _reverse_pass(builder: GraphBuilder, seed: dict[int, int], upto: int) -> dict[int, int]:
    """Accumulate adjoints for nodes ``[0, upto)`` given seed adjoints.

    Returns a map ``node_id -> adjoint node id``. Nodes created by backward
    rules (id >= upto) are part of the new gradient subgraph and are not
    themselves processed in this pass.
    """
    adjoints: dict[int, int] = {}

    def accumulate(nid: int, adj: int) -> None:
        if nid in adjoints:
            adjoints[nid] = builder.add_node("add", [adjoints[nid], adj])
        else:
            adjoints[nid] = adj

    for nid, adj in seed.items():
        accumulate(nid, adj)

    for nid in range(upto - 1, -1, -1):
        adj = adjoints.get(nid)
        if adj is None:
            continue  # not on any path from the seeded output
        node = builder.nodes[nid]
        op = OPS[node.op]
        ctx = BackwardCtx(builder=builder, g=adj, out=nid, inputs=node.inputs, params=node.params)
        contributions = op.backward(ctx)
        if len(contributions) != len(node.inputs):  # pragma: no cover - defensive
            raise AssertionError(f"backward arity mismatch for op {node.op}")
        for inp, sub in zip(node.inputs, contributions):
            if sub is not None:
                accumulate(inp, sub)
    return adjoints


@dataclass(frozen=True)
class HvpProgram:
    """One combined evaluation: value, gradient and HVP in a single pass."""

    nodes: tuple[Node, ...]
    input_node_ids: tuple[int, ...]
    value_id: int
    grad_ids: tuple[int, ...]   # one per input slot, layout order
    hvp_ids: tuple[int, ...]    # one per input slot, layout order
    forward_nodes: int
    gradient_nodes: int
    total_nodes: int


def build_hvp_program(graph: Graph, vector: np.ndarray, subgradient: float) -> HvpProgram:
    """Extend ``graph`` into a combined value/gradient/HVP program.

    ``vector`` is the flat HVP direction in layout order; it is baked into
    the program as constants. ``subgradient`` is the value used at kinks of
    non-smooth ops when the request policy is "subgradient".
    """
    builder = GraphBuilder(graph.nodes, build_options={"subgradient": subgradient})
    forward_nodes = len(builder.nodes)

    # Pass 1: gradient of f w.r.t. each input slot.
    one = builder.const(1.0)
    grads = _reverse_pass(builder, {graph.output: one}, forward_nodes)
    grad_ids = tuple(
        grads.get(nid) if grads.get(nid) is not None else builder.add_node("zeros_like", [nid])
        for nid in graph.input_node_ids
    )
    gradient_nodes = len(builder.nodes) - forward_nodes

    # Scalar phi = <grad f, v>; constant v slices per slot.
    terms: list[int] = []
    for slot, g in zip(graph.layout.slots, grad_ids):
        v_slice = vector[slot.offset : slot.offset + slot.size].reshape(slot.shape)
        v_const = builder.const(v_slice)
        terms.append(builder.add_node("sum", [builder.add_node("mul", [g, v_const])]))
    phi = terms[0]
    for term in terms[1:]:
        phi = builder.add_node("add", [phi, term])

    # Pass 2: gradient of phi w.r.t. each input slot == H @ v.
    upto = len(builder.nodes)
    one2 = builder.const(1.0)
    hvp_grads = _reverse_pass(builder, {phi: one2}, upto)
    hvp_ids = tuple(
        hvp_grads.get(nid) if hvp_grads.get(nid) is not None else builder.add_node("zeros_like", [nid])
        for nid in graph.input_node_ids
    )

    return HvpProgram(
        nodes=tuple(builder.nodes),
        input_node_ids=graph.input_node_ids,
        value_id=graph.output,
        grad_ids=grad_ids,
        hvp_ids=hvp_ids,
        forward_nodes=forward_nodes,
        gradient_nodes=gradient_nodes,
        total_nodes=len(builder.nodes),
    )
