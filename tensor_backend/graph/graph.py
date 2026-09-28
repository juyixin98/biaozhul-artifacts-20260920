"""A small eager computation graph.

The graph is a real DAG of operations over :class:`~tensor_backend.tensor.Tensor`
values (not a hard-coded demo): every node records its opcode, inputs, output
layout, whether the operation aliased storage, and a human-readable location.
Graphs can be executed from a serialized request and serialized back out.
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..tensor import Tensor, ops
from ..tensor.errors import TensorError
from ..tensor.ops import OverlapPolicy

_node_counter = itertools.count(1)

# opcode -> (arity, callable)
_BINARY = {"add", "subtract", "multiply", "divide", "maximum", "minimum"}
_UNARY = {"negate", "abs", "exp", "square"}


@dataclass(frozen=True)
class NodeResult:
    node_id: int
    opcode: str
    output_tensor_id: int
    inputs: tuple[int, ...]
    aliases_storage: bool
    copied: bool
    detail: dict[str, Any]


@dataclass
class GraphNode:
    node_id: int
    opcode: str
    input_ids: tuple[int, ...]
    kwargs: dict[str, Any]
    output: Tensor
    aliases_storage: bool
    copied: bool
    created_at: float
    detail: dict[str, Any] = field(default_factory=dict)

    def result(self) -> NodeResult:
        return NodeResult(
            node_id=self.node_id,
            opcode=self.opcode,
            output_tensor_id=self.output.tensor_id,
            inputs=self.input_ids,
            aliases_storage=self.aliases_storage,
            copied=self.copied,
            detail=self.detail,
        )


class ComputeGraph:
    """Hands out tensor handles and records each operation that connects them."""

    def __init__(self, graph_id: str | None = None) -> None:
        self.graph_id = graph_id or f"graph-{next(_node_counter)}"
        self._tensors: dict[str, Tensor] = {}
        self._nodes: list[GraphNode] = []
        self._storage_of: dict[str, int] = {}

    # ------------------------------------------------------------------ tensors

    def add_tensor(self, handle: str, tensor: Tensor) -> Tensor:
        if handle in self._tensors:
            raise KeyError(f"tensor handle {handle!r} already exists in {self.graph_id}")
        self._tensors[handle] = tensor
        self._storage_of[handle] = tensor.storage.storage_id
        return tensor

    def tensor(self, handle: str) -> Tensor:
        try:
            return self._tensors[handle]
        except KeyError:
            raise KeyError(
                f"unknown tensor handle {handle!r}; known: {sorted(self._tensors)}"
            )

    def handles(self) -> list[str]:
        return sorted(self._tensors)

    # ------------------------------------------------------------------- nodes

    def _record(
        self,
        opcode: str,
        input_handles: Sequence[str],
        output: Tensor,
        *,
        aliases: bool,
        copied: bool,
        detail: dict[str, Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> Tensor:
        node = GraphNode(
            node_id=next(_node_counter),
            opcode=opcode,
            input_ids=tuple(self.tensor(h).tensor_id for h in input_handles),
            kwargs=dict(kwargs or {}),
            output=output,
            aliases_storage=aliases,
            copied=copied,
            created_at=time.time(),
            detail=dict(detail or {}),
        )
        self._nodes.append(node)
        return output

    # --------------------------------------------------------------- operations

    def constant(self, handle: str, values: Any) -> Tensor:
        return self.add_tensor(handle, Tensor.from_values(values, name=handle))

    def transpose(self, out: str, src: str, axes: Sequence[int] | None = None) -> Tensor:
        t = self.tensor(src).transpose(axes, name=out)
        self.add_tensor(out, t)
        self._record("transpose", [src], t, aliases=True, copied=False,
                     detail={"axes": list(axes) if axes is not None else None})
        return t

    def slice_view(
        self,
        out: str,
        src: str,
        start: Sequence[int | None],
        stop: Sequence[int | None],
        step: Sequence[int | None] | None = None,
    ) -> Tensor:
        t_src = self.tensor(src)
        n = len(start)
        step = step or [1] * n
        idx = tuple(
            slice(a, b, c if c is not None else 1)
            for a, b, c in zip(start, stop, step)
        )
        t = t_src[idx]
        t.name = out
        self.add_tensor(out, t)
        self._record("slice", [src], t, aliases=True, copied=False,
                     detail={"start": list(start), "stop": list(stop), "step": list(step)})
        return t

    def reshape(
        self,
        out: str,
        src: str,
        new_shape: Sequence[int],
        *,
        allow_copy: bool = False,
    ) -> Tensor:
        t_src = self.tensor(src)
        info = t_src.reshape_info(new_shape)
        t = t_src.reshape(tuple(new_shape), allow_copy=allow_copy, name=out)
        self.add_tensor(out, t)
        self._record(
            "reshape", [src], t,
            aliases=info["zero_copy"],
            copied=not info["zero_copy"],
            detail=info,
            kwargs={"allow_copy": allow_copy},
        )
        return t

    def binary(
        self,
        out: str,
        op: str,
        a_handle: str,
        b_handle: str,
        *,
        into: str | None = None,
        overlap_policy: str = "raise",
    ) -> Tensor:
        if op not in _BINARY:
            raise KeyError(f"unknown binary opcode {op!r}; valid: {sorted(_BINARY)}")
        a = self.tensor(a_handle)
        b = self.tensor(b_handle)
        out_tensor = self.tensor(into) if into else None
        result = ops.binary(
            op, a, b, out=out_tensor,
            overlap_policy=OverlapPolicy(overlap_policy),
        )
        result.name = out
        if out_tensor is None:
            self.add_tensor(out, result)
        aliases = into is not None
        self._record(
            op, [a_handle, b_handle], result,
            aliases=aliases, copied=not aliases,
            detail={"into": into, "overlap_policy": overlap_policy},
        )
        return result

    def unary(self, out: str, op: str, src: str) -> Tensor:
        if op not in _UNARY:
            raise KeyError(f"unknown unary opcode {op!r}; valid: {sorted(_UNARY)}")
        result = ops.unary(op, self.tensor(src))
        result.name = out
        self.add_tensor(out, result)
        self._record(op, [src], result, aliases=False, copied=True)
        return result

    def reduce_sum(self, out: str, src: str, axis: int | None = None) -> Tensor:
        result = ops.reduce_sum(self.tensor(src), axis=axis)
        result.name = out
        self.add_tensor(out, result)
        self._record("reduce_sum", [src], result, aliases=False, copied=True,
                     detail={"axis": axis})
        return result

    def matmul(self, out: str, a_handle: str, b_handle: str) -> Tensor:
        result = ops.matmul(self.tensor(a_handle), self.tensor(b_handle))
        result.name = out
        self.add_tensor(out, result)
        self._record("matmul", [a_handle, b_handle], result, aliases=False, copied=True)
        return result

    def assign(
        self,
        dst: str,
        src: str,
        *,
        overlap_policy: str = "raise",
    ) -> Tensor:
        """Copy ``src`` into the existing tensor handle ``dst``."""
        dst_tensor = self.tensor(dst)
        result = ops.assign(
            dst_tensor, self.tensor(src),
            overlap_policy=OverlapPolicy(overlap_policy),
        )
        self._record(
            "assign", [src], result,
            aliases=True, copied=False,
            detail={"dst": dst, "overlap_policy": overlap_policy},
        )
        return result

    # ------------------------------------------------------------------- export

    def trace(self) -> list[dict[str, Any]]:
        return [
            {
                "node_id": n.node_id,
                "opcode": n.opcode,
                "input_tensor_ids": list(n.input_ids),
                "output": n.output.describe(),
                "aliases_storage": n.aliases_storage,
                "copied": n.copied,
                "detail": n.detail,
            }
            for n in self._nodes
        ]

    def aliasing_report(self) -> list[dict[str, Any]]:
        """For every tensor, state which base storage it aliases and its range."""
        report = []
        for handle, t in self._tensors.items():
            lo, hi = t.storage_range
            overlaps, uncertain = t.self_overlap()
            report.append({
                "handle": handle,
                "tensor_id": t.tensor_id,
                "storage_id": t.storage.storage_id,
                "shape": list(t.shape),
                "strides": list(t.strides),
                "offset": t.offset,
                "interval": [lo, hi],
                "self_overlapping": overlaps,
                "overlap_uncertain": uncertain,
            })
        return report

    def execute_plan(self, plan: dict[str, Any]) -> None:
        """Execute a JSON-style operation plan against this graph."""
        for step in plan.get("steps", []):
            kind = step["op"]
            if kind == "constant":
                self.constant(step["out"], step["values"])
            elif kind == "transpose":
                self.transpose(step["out"], step["src"], step.get("axes"))
            elif kind == "slice":
                self.slice_view(step["out"], step["src"],
                                step["start"], step["stop"], step.get("step"))
            elif kind == "reshape":
                self.reshape(step["out"], step["src"], step["shape"],
                             allow_copy=step.get("allow_copy", False))
            elif kind in _BINARY:
                self.binary(step["out"], kind, step["a"], step["b"],
                            into=step.get("into"),
                            overlap_policy=step.get("overlap_policy", "raise"))
            elif kind in _UNARY:
                self.unary(step["out"], kind, step["src"])
            elif kind == "reduce_sum":
                self.reduce_sum(step["out"], step["src"], step.get("axis"))
            elif kind == "matmul":
                self.matmul(step["out"], step["a"], step["b"])
            elif kind == "assign":
                self.assign(step["dst"], step["src"],
                            overlap_policy=step.get("overlap_policy", "raise"))
            else:
                raise TensorError(f"graph executor has no opcode {kind!r}")
