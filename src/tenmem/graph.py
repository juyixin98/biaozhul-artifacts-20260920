"""Computation graph: nodes, tensor provenance, wave schedule and validation.

Execution contract
------------------
Nodes are scheduled in *waves* (topological levels). Wave 0 holds externally
supplied tensors (inputs and constants). A node producing tensor ``t`` at wave
``L`` makes ``t`` resident from wave ``L``; ``t`` dies after the greatest wave
in which one of its consumers runs. Graph outputs never die (client releases
them explicitly).

The planner promises that two tensors placed at the same memory location have
disjoint wave intervals, so the plan is safe for *any* execution that respects
wave barriers — including parallel execution of the independent nodes inside a
wave. Sequential execution within a wave is just a conservative special case.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import GraphValidationError
from .ops import get_op
from .tensor import TensorSpec

# Sentinel death wave for graph outputs: alive until the client releases them.
PINNED_DEATH = 10**9


@dataclass(frozen=True)
class Node:
    id: str
    op: str
    inputs: tuple[str, ...]
    outputs: tuple[TensorSpec, ...]
    config: dict = field(default_factory=dict)
    # (output tensor name, input tensor name): output is an alias of that input
    # (in-place / view). Both names then forcibly share one allocation for the
    # hull of their lifetimes.
    aliases: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Graph:
    name: str
    inputs: tuple[TensorSpec, ...]
    nodes: tuple[Node, ...]
    output_names: tuple[str, ...]
    constants: tuple[TensorSpec, ...] = ()

    def spec(self, name: str) -> TensorSpec:
        return self._specs[name]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "inputs": [s.to_dict() for s in self.inputs],
            "constants": [s.to_dict() for s in self.constants],
            "nodes": [
                {
                    "id": n.id,
                    "op": n.op,
                    "inputs": list(n.inputs),
                    "outputs": [o.to_dict() for o in n.outputs],
                    "config": n.config,
                    "aliases": [list(a) for a in n.aliases],
                }
                for n in self.nodes
            ],
            "outputs": list(self.output_names),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Graph":
        try:
            nodes = tuple(
                Node(
                    id=str(n["id"]),
                    op=str(n["op"]),
                    inputs=tuple(n["inputs"]),
                    outputs=tuple(TensorSpec.from_dict(o) for o in n["outputs"]),
                    config=dict(n.get("config", {})),
                    aliases=tuple((a[0], a[1]) for a in n.get("aliases", [])),
                )
                for n in data["nodes"]
            )
            return cls(
                name=str(data.get("name", "graph")),
                inputs=tuple(TensorSpec.from_dict(s) for s in data["inputs"]),
                constants=tuple(TensorSpec.from_dict(s) for s in data.get("constants", [])),
                nodes=nodes,
                output_names=tuple(data["outputs"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GraphValidationError(f"malformed graph definition: {exc}") from exc


@dataclass(frozen=True)
class Schedule:
    waves: tuple[tuple[str, ...], ...]          # node ids per wave
    node_wave: dict[str, int]
    tensor_birth: dict[str, int]                # inputs/constants -> 0
    producer: dict[str, str | None]             # tensor -> producing node id


def validate_and_schedule(graph: Graph) -> Schedule:
    """Validate the graph and derive the wave schedule.

    Raises :class:`GraphValidationError` (category ``input_error``) on any
    structural problem: duplicate names, unknown op/arity, dangling input,
    cycle, bad alias, unknown output.
    """
    input_names = [s.name for s in graph.inputs]
    const_names = [s.name for s in graph.constants]
    if len(set(input_names)) != len(input_names):
        raise GraphValidationError("duplicate graph input names")
    if set(const_names) & set(input_names):
        raise GraphValidationError("constant name collides with an input")

    producer: dict[str, str | None] = {n: None for n in input_names + const_names}
    node_ids: set[str] = set()

    # Pass 1: register every node and every produced tensor name.
    for node in graph.nodes:
        if node.id in node_ids or node.id in producer:
            raise GraphValidationError(f"duplicate node/id name {node.id!r}")
        node_ids.add(node.id)
        try:
            op = get_op(node.op)
        except KeyError:
            raise GraphValidationError(f"unknown op {node.op!r} in node {node.id!r}")
        if len(node.inputs) != op.n_inputs:
            raise GraphValidationError(
                f"node {node.id!r} op {node.op!r} expects {op.n_inputs} inputs, "
                f"got {len(node.inputs)}"
            )
        if len(node.outputs) != op.n_outputs:
            raise GraphValidationError(
                f"node {node.id!r} op {node.op!r} expects {op.n_outputs} outputs, "
                f"got {len(node.outputs)}"
            )
        for spec in node.outputs:
            if spec.name in producer:
                raise GraphValidationError(f"tensor {spec.name!r} produced more than once")
        for spec in node.outputs:
            producer[spec.name] = node.id

    all_tensors = set(producer)

    # Pass 2: validate references now that the whole name space is known.
    for node in graph.nodes:
        op = get_op(node.op)
        for inp in node.inputs:
            if inp not in all_tensors:
                raise GraphValidationError(
                    f"node {node.id!r} references undefined tensor {inp!r}"
                )
        for out_name, in_name in node.aliases:
            if out_name not in [o.name for o in node.outputs]:
                raise GraphValidationError(f"alias in {node.id!r}: {out_name!r} is not its output")
            if in_name not in node.inputs:
                raise GraphValidationError(f"alias in {node.id!r}: {in_name!r} is not its input")
            out_spec = next(o for o in node.outputs if o.name == out_name)
            in_spec = _resolve_spec(graph, producer, in_name)
            if out_spec.dtype != in_spec.dtype:
                raise GraphValidationError(
                    f"alias in {node.id!r}: dtype mismatch {out_spec.name}/{in_spec.name}"
                )
        if node.op in ("add", "mul"):
            for inp in node.inputs:
                in_spec = _resolve_spec(graph, producer, inp)
                if in_spec.dtype != node.outputs[0].dtype:
                    raise GraphValidationError(
                        f"node {node.id!r}: dtype of input {inp!r} does not match output"
                    )
        if node.op == "matmul":
            a = _resolve_spec(graph, producer, node.inputs[0])
            b = _resolve_spec(graph, producer, node.inputs[1])
            if len(a.bounds) != 2 or len(b.bounds) != 2:
                raise GraphValidationError(f"node {node.id!r}: matmul requires rank 2")
            if a.bounds[1] != b.bounds[0]:
                raise GraphValidationError(
                    f"node {node.id!r}: matmul contraction bounds differ "
                    f"{a.bounds[1]} vs {b.bounds[0]}"
                )

    for name in graph.output_names:
        if name not in producer:
            raise GraphValidationError(f"graph output {name!r} does not exist")

    # Wave assignment (longest-path levels). Kahn-style propagation in the
    # declared node order; the order must itself be topological.
    tensor_birth = {n: 0 for n in input_names + const_names}
    remaining: dict[str, set[str]] = {}
    for node in graph.nodes:
        deps = {produced_by(graph, i) for i in node.inputs if produced_by(graph, i) is not None}
        remaining[node.id] = deps

    wave_of_node: dict[str, int] = {}
    order: list[str] = []
    done: set[str] = set()
    # Respect declared order but verify topological validity.
    for node in graph.nodes:
        deps = remaining[node.id]
        if not deps <= done:
            raise GraphValidationError(
                f"node {node.id!r} appears before its dependencies "
                f"{sorted(deps - done)}; nodes must be topologically ordered"
            )
        wave = 1 + max((wave_of_node[d] for d in deps), default=-1)
        wave_of_node[node.id] = wave
        for spec in node.outputs:
            tensor_birth[spec.name] = wave
        done.add(node.id)
        order.append(node.id)

    if len(order) != len(graph.nodes):  # pragma: no cover - defensive
        raise GraphValidationError("graph contains a cycle")

    max_wave = max(wave_of_node.values(), default=-1)
    waves = tuple(
        tuple(n.id for n in graph.nodes if wave_of_node[n.id] == w)
        for w in range(max_wave + 1)
    )
    return Schedule(
        waves=waves,
        node_wave=wave_of_node,
        tensor_birth=tensor_birth,
        producer=producer,
    )


def produced_by(graph: Graph, tensor: str) -> str | None:
    for node in graph.nodes:  # pragma-ish small graphs; cached via schedule.producer
        if any(o.name == tensor for o in node.outputs):
            return node.id
    return None


def _resolve_spec(graph: Graph, producer: dict[str, str | None], name: str) -> TensorSpec:
    src = producer[name]
    if src is None:
        return next(s for s in (*graph.inputs, *graph.constants) if s.name == name)
    node = next(n for n in graph.nodes if n.id == src)
    return next(o for o in node.outputs if o.name == name)
