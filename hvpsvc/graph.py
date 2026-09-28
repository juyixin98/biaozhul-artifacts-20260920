"""Scalar computation graph.

Every expression is a scalar function of tensor-valued input variables.
Internally variables are flattened (see :mod:`hvpsvc.tensor`), so graph
*leaves* are scalar components named ``"\x00leaf:<var>[i]"``.

Two roles share one class:

- The user-supplied **primal** expression: leaves + ops, one output root.
- The **gradient expression** built by reverse mode: literal constants are
  allowed and there are several roots (one gradient component). All gradient
  components live in one shared graph so common subexpressions are stored and
  evaluated only once.

Node ids declared by the user are used verbatim. Tensor components pulled out
with the ``get`` pseudo-op are recorded in an alias table (user id -> leaf
node), so the evaluator only ever visits each canonical node once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

import numpy as np

from . import ops as ops_mod
from .errors import ComputeFailureError, InputError, ResourceExhaustedError

DTYPE = np.float64
LEAF_PREFIX = "\x00leaf:"  # internal leaf id, never collides with user ids


def leaf_id(var_name: str, flat_index: int) -> str:
    return f"{LEAF_PREFIX}{var_name}[{flat_index}]"


def is_leaf_id(node_id: str) -> bool:
    return node_id.startswith(LEAF_PREFIX)


class NodeKind(str, Enum):
    LEAF = "leaf"
    CONST = "const"
    OP = "op"


@dataclass
class Node:
    id: str
    kind: NodeKind
    inputs: tuple[str, ...] = ()
    op: str | None = None
    value: float | None = None  # const: literal value
    var_name: str | None = None  # leaf: owning variable
    flat_index: int | None = None  # leaf: index inside that variable's flat vector


@dataclass
class Budget:
    """Computational budget. Exceedance raises ResourceExhaustedError."""

    max_nodes: int = 200_000
    max_evals: int = 2_000_000
    deadline_monotonic: float | None = None
    nodes_used: int = 0
    evals_used: int = 0

    def tick_node(self) -> None:
        self.nodes_used += 1
        if self.nodes_used > self.max_nodes:
            raise ResourceExhaustedError(
                f"graph node budget exceeded: {self.nodes_used} > {self.max_nodes}",
                detail={"limit": "max_nodes", "used": self.nodes_used,
                        "limit_value": self.max_nodes},
            )

    def tick_eval(self, n: int = 1) -> None:
        import time

        self.evals_used += n
        if self.evals_used > self.max_evals:
            raise ResourceExhaustedError(
                f"evaluation budget exceeded: {self.evals_used} > {self.max_evals}",
                detail={"limit": "max_evals", "used": self.evals_used,
                        "limit_value": self.max_evals},
            )
        if self.deadline_monotonic is not None and time.monotonic() > self.deadline_monotonic:
            raise ResourceExhaustedError(
                "wall-clock budget exceeded",
                detail={"limit": "wall_clock", "evals_used": self.evals_used},
            )


@dataclass(frozen=True)
class NonsmoothConfig:
    """Behaviour at kinks (abs/relu/max/min/sign at the non-diff point).

    - ``reject``      -> NonSmoothError with the exact kink location.
    - ``subgradient`` -> use the designated subderivative ``subgradient``.
      For abs/sign that value is mapped to [-1, 1] via 2g-1; for relu/max/min
      it is used directly from the convex subgradient range [0, 1].
    """

    policy: str = "reject"
    subgradient: float = 0.0

    def __post_init__(self) -> None:
        if self.policy not in ("reject", "subgradient"):
            raise InputError(f"invalid nonsmooth policy: {self.policy!r}",
                             detail={"policy": self.policy})
        if self.policy == "subgradient" and not (0.0 <= float(self.subgradient) <= 1.0):
            raise InputError(
                "subgradient must lie in [0, 1] (mapped per op)",
                detail={"subgradient": self.subgradient},
            )


class ExprGraph:
    """Builder + container for a symbolic scalar expression graph."""

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.order: list[str] = []          # canonical construction order
        self.aliases: dict[str, str] = {}   # user id -> canonical id
        self._const_cache: dict[float, str] = {}
        self._counter = 0

    # ---- resolution ---------------------------------------------------
    def resolve(self, ref: str) -> str:
        seen: set[str] = set()
        while ref in self.aliases and ref not in seen:
            seen.add(ref)
            ref = self.aliases[ref]
        return ref

    def has(self, ref: str) -> bool:
        return ref in self.nodes or ref in self.aliases

    # ---- construction --------------------------------------------------
    def _anon(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}#{self._counter}"

    def leaf(self, var_name: str, flat_index: int) -> str:
        lid = leaf_id(var_name, flat_index)
        if lid not in self.nodes:
            self.nodes[lid] = Node(id=lid, kind=NodeKind.LEAF,
                                   var_name=var_name, flat_index=flat_index)
            self.order.append(lid)
        return lid

    def constant(self, value: float, nid: str | None = None) -> str:
        value = float(value)
        if nid is None:
            if value in self._const_cache:
                return self._const_cache[value]
            nid = self._anon("const")
        node = Node(id=nid, kind=NodeKind.CONST, value=value)
        self._register(nid, node)
        self._const_cache[value] = nid
        return nid

    def apply(self, op_name: str, inputs: Sequence[str],
              nid: str | None = None) -> str:
        spec = ops_mod.get(op_name)
        if len(inputs) != spec.arity:
            raise InputError(
                f"op {op_name!r} expects {spec.arity} inputs, got {len(inputs)}",
                detail={"op": op_name, "expected_arity": spec.arity,
                        "actual_arity": len(inputs)},
            )
        canon = tuple(self.resolve(i) for i in inputs)
        for i in canon:
            if i not in self.nodes:
                raise InputError(f"unknown node reference {i!r}",
                                 detail={"node": i})
        if nid is None:
            nid = self._anon(op_name)
        node = Node(id=nid, kind=NodeKind.OP, op=op_name, inputs=canon)
        self._register(nid, node)
        return nid

    def alias(self, alias_id: str, canonical: str) -> None:
        if alias_id in self.nodes or alias_id in self.aliases:
            raise InputError(f"duplicate node id {alias_id!r}",
                             detail={"node": alias_id})
        self.aliases[alias_id] = self.resolve(canonical)

    def _register(self, nid: str, node: Node) -> None:
        if nid in self.nodes or nid in self.aliases:
            raise InputError(f"duplicate node id {nid!r}",
                             detail={"node": nid})
        self.nodes[nid] = node
        self.order.append(nid)

    # ---- evaluation ----------------------------------------------------
    def _leaf_value(self, node: Node, x: np.ndarray, offsets: dict[str, int]) -> float:
        return float(x[offsets[node.var_name] + node.flat_index])  # type: ignore[operator]

    def evaluate(
        self,
        roots: Sequence[str],
        x: np.ndarray,
        layout,
        *,
        budget: Budget,
        stage: str,
    ) -> np.ndarray:
        """Evaluate canonical ``roots`` at flat point ``x`` with finite checks."""
        offsets = layout.offsets()
        values: dict[str, float] = {}
        for nid in self.order:
            budget.tick_eval()
            node = self.nodes[nid]
            if node.kind is NodeKind.LEAF:
                val = self._leaf_value(node, x, offsets)
            elif node.kind is NodeKind.CONST:
                val = float(node.value)  # type: ignore[arg-type]
            else:
                invals = [values[i] for i in node.inputs]
                val = safe_forward(node.op, invals, nid, stage)
            if not np.isfinite(val):
                raise ComputeFailureError(
                    f"non-finite value at node {nid!r} during {stage}",
                    detail={"stage": stage, "node": nid, "op": node.op,
                            "value": val},
                )
            values[nid] = val
        canon_roots = [self.resolve(r) for r in roots]
        return np.asarray([values[r] for r in canon_roots], dtype=DTYPE)

    def root_value_map(
        self,
        roots: Sequence[str],
        x: np.ndarray,
        layout,
        *,
        budget: Budget,
        stage: str,
    ) -> dict[str, float]:
        vals = self.evaluate(roots, x, layout, budget=budget, stage=stage)
        return {r: float(v) for r, v in zip(roots, vals)}


def safe_forward(op_name: str | None, invals: list[float],
                 nid: str, stage: str) -> float:
    """Forward an op with domain/overflow protection and finite checks."""
    spec = ops_mod.get(op_name)
    with np.errstate(all="ignore"):
        try:
            val = float(spec.forward(invals))
        except (ZeroDivisionError, ValueError, FloatingPointError, OverflowError) as exc:
            raise ComputeFailureError(
                f"domain error in op {op_name!r} at node {nid!r} during {stage}: {exc}",
                detail={"stage": stage, "node": nid, "op": op_name,
                        "inputs": invals},
            ) from exc
    if not np.isfinite(val):
        raise ComputeFailureError(
            f"non-finite result from op {op_name!r} at node {nid!r} during {stage}",
            detail={"stage": stage, "node": nid, "op": op_name,
                    "inputs": invals, "value": val},
        )
    return val


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------

def build_primal_from_spec(
    spec: dict[str, Any],
    layout,
    *,
    budget: Budget,
) -> tuple[ExprGraph, str]:
    """Parse a JSON expression spec.

    Spec shape::

        {"nodes": [
            {"id": "xi", "op": "get", "args": ["x"], "index": 2},
            {"id": "n1", "op": "add", "args": ["x", "xi"]},
            {"id": "s",  "op": "scalar", "value": 2.5}
         ],
         "output": "n1"}

    Bare references to scalar variables are allowed directly in ``args``.
    Tensor variables must be pulled out with the ``get`` pseudo-op.
    Constants can be injected with the pseudo-op ``scalar`` (mainly useful
    so specs are self-contained; arithmetic literals also work via args).
    """
    graph = ExprGraph()
    nodes_spec = spec.get("nodes")
    if not isinstance(nodes_spec, list) or not nodes_spec:
        raise InputError("'nodes' must be a non-empty list")

    declared: set[str] = set(layout.names())

    def use_scalar_variable(name: str, owner: str) -> str:
        lay = layout.layout(name)
        if not lay.is_scalar:
            raise InputError(
                f"node {owner!r}: tensor variable {name!r} must be indexed "
                "with a 'get' node before use",
                detail={"node": owner, "variable": name},
            )
        return graph.leaf(name, 0)

    for k, ns in enumerate(nodes_spec):
        if not isinstance(ns, dict):
            raise InputError(f"node #{k} must be an object")
        nid = ns.get("id")
        if not isinstance(nid, str) or not nid:
            raise InputError(f"node #{k} requires a non-empty string 'id'")
        if is_leaf_id(nid):
            raise InputError(f"reserved node id {nid!r}", detail={"node": nid})
        if nid in declared:
            raise InputError(f"duplicate node id {nid!r}", detail={"node": nid})
        op_name = ns.get("op")
        if not isinstance(op_name, str):
            raise InputError(f"node {nid!r} requires a string 'op'")
        args = ns.get("args", [])
        if not isinstance(args, list):
            raise InputError(f"node {nid!r}: 'args' must be a list")

        if op_name == "get":
            if len(args) != 1 or not isinstance(args[0], str):
                raise InputError(
                    f"node {nid!r}: 'get' requires exactly one variable arg",
                    detail={"node": nid})
            name = args[0]
            lay = layout.layout(name)  # raises InputError if unknown
            index = ns.get("index")
            if isinstance(index, bool) or not isinstance(index, int):
                raise InputError(
                    f"node {nid!r}: 'get' requires an integer 'index'",
                    detail={"node": nid})
            if not 0 <= index < lay.size:
                raise InputError(
                    f"node {nid!r}: index {index} out of range for variable "
                    f"{name!r} of size {lay.size}",
                    detail={"node": nid, "variable": name, "index": index,
                            "size": lay.size})
            canonical = graph.leaf(name, index)
            if nid != canonical:
                graph.alias(nid, canonical)
            declared.add(nid)
            budget.tick_node()
            continue

        if op_name == "scalar":
            value = ns.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InputError(
                    f"node {nid!r}: 'scalar' requires a numeric 'value'",
                    detail={"node": nid})
            graph.constant(float(value), nid=nid)
            declared.add(nid)
            budget.tick_node()
            continue

        spec_op = ops_mod.get(op_name)  # unknown op -> InputError
        if len(args) != spec_op.arity:
            raise InputError(
                f"node {nid!r}: op {op_name!r} expects {spec_op.arity} args, "
                f"got {len(args)}",
                detail={"node": nid, "op": op_name,
                        "expected_arity": spec_op.arity,
                        "actual_arity": len(args)})
        canon_inputs: list[str] = []
        for j, arg in enumerate(args):
            if not isinstance(arg, str):
                raise InputError(
                    f"node {nid!r}: arg #{j} must be a string reference",
                    detail={"node": nid, "arg_index": j})
            if graph.has(arg):
                canon_inputs.append(graph.resolve(arg))
            elif arg in declared:
                canon_inputs.append(use_scalar_variable(arg, nid))
            else:
                raise InputError(
                    f"node {nid!r}: unknown arg {arg!r} "
                    "(not a prior node or declared variable)",
                    detail={"node": nid, "arg": arg})
        graph.apply(op_name, canon_inputs, nid=nid)
        declared.add(nid)
        budget.tick_node()

    output = spec.get("output")
    if not isinstance(output, str) or not graph.has(output):
        raise InputError("'output' must reference a declared node or variable",
                         detail={"output": output})
    return graph, graph.resolve(output)
