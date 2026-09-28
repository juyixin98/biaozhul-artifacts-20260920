"""Ordered parameter graph.

The graph fixes *which* tensors exist and *in what order* for the whole
lifetime of a job.  Bucket layout, flat packing, and the reduction all key
off this order, so it is built once, frozen, and never mutated per round.
A parameter may be marked non-trainable (e.g. a frozen bias): it keeps its
slot in the order and participates as an explicit placeholder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterator, List, Tuple

from bucket_sync.tensor_types import TensorSpec


class GraphError(ValueError):
    """Raised when a parameter graph is malformed or misused."""


@dataclass(frozen=True)
class ParameterNode:
    """One parameter in the graph."""

    spec: TensorSpec
    trainable: bool = True


class ParameterGraph:
    """An immutable, ordered set of named parameters.

    The order is the insertion order given at construction and is part of
    the job's identity: two runs with the same graph name and same specs
    must produce byte-identical bucket layouts.
    """

    def __init__(self, name: str, nodes: List[ParameterNode]) -> None:
        if not name or not name.strip():
            raise GraphError("graph name must be a non-empty string")
        if not nodes:
            raise GraphError("graph must contain at least one parameter")
        seen: set[str] = set()
        for node in nodes:
            if node.spec.name in seen:
                raise GraphError(f"duplicate parameter name {node.spec.name!r}")
            seen.add(node.spec.name)
        self._name = name
        self._nodes: Tuple[ParameterNode, ...] = tuple(nodes)
        self._by_name: Dict[str, ParameterNode] = {n.spec.name: n for n in self._nodes}

    @property
    def name(self) -> str:
        return self._name

    @property
    def nodes(self) -> Tuple[ParameterNode, ...]:
        return self._nodes

    def __iter__(self) -> Iterator[ParameterNode]:
        return iter(self._nodes)

    def __len__(self) -> int:
        return len(self._nodes)

    def node(self, param_name: str) -> ParameterNode:
        try:
            return self._by_name[param_name]
        except KeyError:
            raise GraphError(
                f"unknown parameter {param_name!r} in graph {self._name!r}"
            ) from None

    def spec(self, param_name: str) -> TensorSpec:
        return self.node(param_name).spec

    def param_names(self) -> Tuple[str, ...]:
        return tuple(n.spec.name for n in self._nodes)

    def trainable_names(self) -> Tuple[str, ...]:
        return tuple(n.spec.name for n in self._nodes if n.trainable)

    def placeholder_names(self) -> Tuple[str, ...]:
        """Parameters that keep a slot but carry no gradient (explicit placeholders)."""
        return tuple(n.spec.name for n in self._nodes if not n.trainable)

    def total_size(self) -> int:
        return sum(n.spec.size for n in self._nodes)

    def fingerprint(self) -> str:
        """Stable string identifying graph + order; used in diagnostics."""
        parts = [
            f"{n.spec.name}:{','.join(map(str, n.spec.shape))}:{n.spec.dtype}"
            f":{'T' if n.trainable else 'P'}"
            for n in self._nodes
        ]
        return f"{self._name}[{'|'.join(parts)}]"
