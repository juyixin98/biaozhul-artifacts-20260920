"""Forward / reverse composition and Hessian-vector products.

Algorithm (forward-over-reverse, Hessian never materialized):

1. **Forward** - evaluate the primal scalar graph at the point ``x``.
2. **Reverse** - build one *symbolic gradient graph*:

   - every primal node has a mirrored value expression ``val:<id>`` (leaves
     remain leaves, so the gradient graph is a genuine function of ``x``);
   - adjoints are accumulated in reverse topological order; a node with
     several consumers receives one ``add`` contribution per consumer edge,
     so shared subgraphs are never skipped or double counted;
   - the roots are the gradient components in input-flat order.

3. **HVP** - forward-mode (tangent / dual-number) propagation through the
   gradient graph with seed ``xdot = v``; the root tangents are ``H f(x) v``.

The cost is one reverse build plus two graph sweeps - independent of the
Hessian dimension and with no n x n matrix anywhere.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from . import ops as ops_mod
from .errors import InputError, NonSmoothError
from .graph import (
    DTYPE,
    Budget,
    ExprGraph,
    NodeKind,
    NonsmoothConfig,
    safe_forward,
)

VAL_PREFIX = "val:"
ACC_PREFIX = "acc:"
ZERO_PREFIX = "zero:"


# ---------------------------------------------------------------------------
# Reverse mode: symbolic gradient graph
# ---------------------------------------------------------------------------

@dataclass
class GradientGraph:
    """Frozen result of the reverse pass for one primal graph/point."""

    expr: ExprGraph
    roots: list[str]                 # gradient components, flat input order
    primal_value: float
    primal_values: dict[str, float]
    kinks: list[dict] = field(default_factory=list)  # kinks encountered
    built_nodes: int = 0
    #: number of adjoint contributions accumulated per primal node. A node
    #: with k consumer edges receives k contributions (shared-subgraph proof).
    contributions: dict[str, int] = field(default_factory=dict)


class _GradientBuilder:
    def __init__(self, graph: ExprGraph, output: str, x: np.ndarray,
                 layout, budget: Budget, ns: NonsmoothConfig) -> None:
        self.g = graph
        self.output = graph.resolve(output)
        self.x = x
        self.layout = layout
        self.budget = budget
        self.ns = ns
        self.gg = ExprGraph()
        self.acc: dict[str, str] = {}
        self.kinks: list[dict] = []
        self.contributions: dict[str, int] = {}

    def build(self) -> GradientGraph:
        # 1) primal forward sweep (also domain/overflow validation)
        vals_arr = self.g.evaluate(
            self.g.order, self.x, self.layout,
            budget=self.budget, stage="forward")
        vals: dict[str, float] = dict(zip(self.g.order, vals_arr))
        primal_value = vals[self.output]

        # 2) mirror every primal node as a symbolic value expression
        for nid in self.g.order:
            node = self.g.nodes[nid]
            mid = VAL_PREFIX + nid
            if node.kind is NodeKind.LEAF:
                # share the very same leaf node
                canon = self.gg.leaf(node.var_name, node.flat_index)  # type: ignore[arg-type]
                if canon != mid:
                    self.gg.alias(mid, canon)
            elif node.kind is NodeKind.CONST:
                self.gg.constant(float(node.value), nid=mid)  # type: ignore[arg-type]
            else:
                self.gg.apply(
                    node.op,  # type: ignore[arg-type]
                    [VAL_PREFIX + i for i in node.inputs],
                    nid=mid)
            self.budget.tick_node()

        # 3) seed output adjoint with the constant 1
        self.acc[self.output] = self.gg.constant(1.0, nid="seed:1")
        self.budget.tick_node()

        # 4) reverse topological accumulation
        for nid in reversed(self.g.order):
            node = self.g.nodes[nid]
            if node.kind is not NodeKind.OP:
                continue
            out_bar = self.acc.get(nid)
            if out_bar is None:
                continue  # unreachable from output on the reverse path
            invals = [float(vals[i]) for i in node.inputs]
            partials = self._local_partials(node.op, invals, nid)  # type: ignore[arg-type]
            for inp, partial in zip(node.inputs, partials):
                contrib = self.gg.apply("mul", [partial, out_bar])
                self.budget.tick_node()
                self._accumulate(inp, contrib)

        # 5) gradient roots in flat layout order (zero for leaves not present)
        roots: list[str] = []
        offsets = self.layout.offsets()
        zero_count = 0
        for var in self.layout.variables:
            for idx in range(var.size):
                from .graph import leaf_id

                lid = leaf_id(var.name, idx)
                if lid in self.acc:
                    roots.append(self.acc[lid])
                else:
                    zid = f"{ZERO_PREFIX}{zero_count}"
                    zero_count += 1
                    roots.append(self.gg.constant(0.0, nid=zid))
                    self.budget.tick_node()

        return GradientGraph(
            expr=self.gg,
            roots=roots,
            primal_value=primal_value,
            primal_values=vals,
            kinks=self.kinks,
            built_nodes=self.budget.nodes_used,
            contributions=self.contributions,
        )

    # -- adjoint accumulation ------------------------------------------
    def _accumulate(self, canonical_input: str, contribution: str) -> None:
        self.contributions[canonical_input] = \
            self.contributions.get(canonical_input, 0) + 1
        cur = self.acc.get(canonical_input)
        if cur is None:
            # name the accumulator after the primal node for readability
            self.gg.alias(ACC_PREFIX + canonical_input, contribution)
            self.acc[canonical_input] = ACC_PREFIX + canonical_input
        else:
            new_acc = self.gg.apply("add", [cur, contribution])
            # keep the stable accumulator name pointing at the latest node
            self.acc[canonical_input] = new_acc

    # -- symbolic local derivative rules -------------------------------
    def _const(self, value: float) -> str:
        return self.gg.constant(float(value))

    def _v(self, canonical_id: str) -> str:
        return VAL_PREFIX + canonical_id

    def _local_partials(self, op: str, invals: list[float],
                        nid: str) -> list[str]:
        g = self.gg
        a = self._v(self.g.nodes[nid].inputs[0])

        if op == "add":
            return [self._const(1.0), self._const(1.0)]
        if op == "sub":
            return [self._const(1.0), self._const(-1.0)]
        if op == "mul":
            b = self._v(self.g.nodes[nid].inputs[1])
            return [b, a]
        if op == "div":
            b = self._v(self.g.nodes[nid].inputs[1])
            return [
                g.apply("div", [self._const(1.0), b]),
                g.apply("neg", [g.apply("div", [a, g.apply("mul", [b, b])])]),
            ]
        if op == "neg":
            return [self._const(-1.0)]

        smooth_unary = {
            "sin": lambda v: g.apply("cos", [v]),
            "cos": lambda v: g.apply("neg", [g.apply("sin", [v])]),
            "exp": lambda v: g.apply("exp", [v]),
            "log": lambda v: g.apply("div", [self._const(1.0), v]),
            "sqrt": lambda v: g.apply(
                "div", [self._const(1.0),
                        g.apply("mul", [self._const(2.0),
                                        g.apply("sqrt", [v])])]),
        }
        if op in smooth_unary:
            return [smooth_unary[op](a)]

        # piecewise-smooth ops
        spec = ops_mod.get(op)
        at_kink = bool(spec.at_kink and spec.at_kink(invals))  # type: ignore[misc]
        if at_kink:
            self.kinks.append({"node": nid, "op": op, "inputs": list(invals)})
            if self.ns.policy == "reject":
                raise NonSmoothError(
                    f"non-differentiable point at node {nid!r} "
                    f"(op {op!r}, inputs={invals}); set nonsmooth.policy="
                    "'subgradient' to pick a designated subderivative",
                    detail={"node": nid, "op": op, "inputs": invals,
                            "policy": "reject"},
                )
            s = self.ns.subgradient
            if spec.arity == 1:
                return [self._const(ops_mod.unary_slope(op, invals, s, nid))]
            sa, sb = ops_mod.binary_slopes(op, invals, s, nid)
            return [self._const(sa), self._const(sb)]

        # smooth branch away from the kink
        if op == "abs":
            return [self._const(1.0 if invals[0] > 0 else -1.0)]
        if op == "relu":
            return [self._const(1.0 if invals[0] > 0 else 0.0)]
        if op == "sign":
            return [self._const(0.0)]
        if op in ("max", "min"):
            # kink (tie) handled above; here inputs differ
            max_active = 0 if invals[0] > invals[1] else 1
            active = max_active if op == "max" else 1 - max_active
            return [self._const(1.0 if active == 0 else 0.0),
                    self._const(1.0 if active == 1 else 0.0)]
        raise InputError(f"no derivative rule registered for op {op!r}",
                         detail={"op": op})


def build_gradient_graph(graph: ExprGraph, output: str, x: np.ndarray,
                         layout, budget: Budget,
                         ns: NonsmoothConfig) -> GradientGraph:
    return _GradientBuilder(graph, output, x, layout, budget, ns).build()


# ---------------------------------------------------------------------------
# Gradient evaluation and HVP via tangent propagation
# ---------------------------------------------------------------------------

def gradient(gg: GradientGraph, x: np.ndarray, layout,
             budget: Budget) -> np.ndarray:
    """Evaluate the symbolic gradient graph at ``x``."""
    return gg.expr.evaluate(gg.roots, x, layout,
                            budget=budget, stage="reverse")


def _tangent_rule(op: str, v: list[float], t: list[float],
                  ns: NonsmoothConfig, nid: str) -> float:
    """Local JVP: given primal input values and tangents, return output tangent."""
    spec = ops_mod.get(op)
    if spec.nonsmooth:
        at_kink = bool(spec.at_kink and spec.at_kink(v))  # type: ignore[misc]
        if at_kink:
            if ns.policy == "reject":
                raise NonSmoothError(
                    f"non-differentiable point at node {nid!r} "
                    f"(op {op!r}, inputs={v}) during HVP",
                    detail={"stage": "hvp", "node": nid, "op": op,
                            "inputs": v, "policy": "reject"})
            s = ns.subgradient
            if spec.arity == 1:
                slope = ops_mod.unary_slope(op, v, s, nid)
                return slope * t[0]
            sa, sb = ops_mod.binary_slopes(op, v, s, nid)
            return sa * t[0] + sb * t[1]

    with np.errstate(all="ignore"):
        if op == "add":
            return t[0] + t[1]
        if op == "sub":
            return t[0] - t[1]
        if op == "neg":
            return -t[0]
        if op == "mul":
            return v[0] * t[1] + v[1] * t[0]
        if op == "div":
            return (t[0] * v[1] - v[0] * t[1]) / (v[1] * v[1])
        if op == "sin":
            return math.cos(v[0]) * t[0]
        if op == "cos":
            return -math.sin(v[0]) * t[0]
        if op == "exp":
            return math.exp(v[0]) * t[0]
        if op == "log":
            return t[0] / v[0]
        if op == "sqrt":
            return t[0] / (2.0 * math.sqrt(v[0]))
        if op == "abs":
            return (1.0 if v[0] > 0 else -1.0) * t[0]
        if op == "relu":
            return t[0] if v[0] > 0 else 0.0
        if op == "sign":
            return 0.0
        if op in ("max", "min"):
            max_active = 0 if v[0] > v[1] else 1
            active = max_active if op == "max" else 1 - max_active
            return t[active]
    raise InputError(f"no tangent rule registered for op {op!r}",
                     detail={"op": op})


def hvp(gg: GradientGraph, x: np.ndarray, v: np.ndarray, layout,
        budget: Budget, ns: NonsmoothConfig) -> np.ndarray:
    """Tangent propagation through the gradient graph -> H f(x) v.

    Primal values are recomputed along the way (mirrored value nodes); each
    op contributes exactly its local JVP. A zero direction ``v`` propagates
    exact zeros, never a perturbed approximation.
    """
    if v.shape != x.shape:
        raise InputError(
            f"vector shape {v.shape} is not bound to the input layout {x.shape}",
            detail={"expected_shape": list(x.shape),
                    "actual_shape": list(v.shape)})

    offsets = layout.offsets()
    values: dict[str, float] = {}
    tangs: dict[str, float] = {}

    for nid in gg.expr.order:
        budget.tick_eval()
        node = gg.expr.nodes[nid]
        if node.kind is NodeKind.LEAF:
            off = offsets[node.var_name]
            idx = node.flat_index
            val = float(x[off + idx])  # type: ignore[operator]
            tan = float(v[off + idx])  # type: ignore[operator]
        elif node.kind is NodeKind.CONST:
            val = float(node.value)  # type: ignore[arg-type]
            tan = 0.0
        else:
            invals = [values[i] for i in node.inputs]
            itans = [tangs[i] for i in node.inputs]
            val = safe_forward(node.op, invals, nid, stage="hvp")
            tan = _tangent_rule(node.op, invals, itans, ns, nid)
        if not np.isfinite(val):
            from .errors import ComputeFailureError

            raise ComputeFailureError(
                f"non-finite value at node {nid!r} during hvp",
                detail={"stage": "hvp", "node": nid, "op": node.op,
                        "value": val})
        if not np.isfinite(tan):
            from .errors import ComputeFailureError

            raise ComputeFailureError(
                f"non-finite tangent at node {nid!r} during hvp (numerical "
                "overflow in HVP propagation)",
                detail={"stage": "hvp", "node": nid, "op": node.op,
                        "tangent": tan})
        values[nid] = val
        tangs[nid] = tan

    roots = [gg.expr.resolve(r) for r in gg.roots]
    return np.asarray([tangs[r] for r in roots], dtype=DTYPE)
