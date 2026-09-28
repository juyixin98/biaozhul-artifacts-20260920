"""Independent eager forward + reverse-mode oracle.

The spec is interpreted directly (no reuse of graph/ops/executor code).
Stochastic nodes receive *fixed externally supplied masks*, so this oracle
differentiates a deterministic function -- the same function the executor
must realise by replaying RNG state.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Tuple

import numpy as np

ROOT_OPS = ("input", "parameter")


class OracleGraph:
    def __init__(self, spec: Mapping[str, Any]) -> None:
        self.spec = spec
        self.nodes: Dict[str, dict] = {n["id"]: n for n in spec["nodes"]}
        self.order: List[str] = self._topo()
        self.target: str = spec["target"]

    def _topo(self) -> List[str]:
        remaining = dict(self.nodes)
        done: set = set()
        order: List[str] = []
        while remaining:
            ready = sorted(
                nid for nid, n in remaining.items()
                if all(r in done for r in n["inputs"])
            )
            if not ready:
                raise ValueError("cycle in fixture spec")
            for nid in ready:
                order.append(nid)
                done.add(nid)
                remaining.pop(nid)
        return order

    # -- forward -----------------------------------------------------------

    def forward(self, params: Mapping[str, np.ndarray],
                inputs: Mapping[str, np.ndarray],
                masks: Mapping[str, np.ndarray]
                ) -> Tuple[float, Dict[str, np.ndarray], Dict[str, tuple]]:
        """Eager forward; returns loss, all node values, saved VJP inputs."""

        values: Dict[str, np.ndarray] = {}
        saved_inputs: Dict[str, tuple] = {}
        for nid in self.order:
            n = self.nodes[nid]
            op = n["op"]
            if op == "input":
                values[nid] = np.asarray(inputs[nid], dtype=np.float64)
            elif op == "parameter":
                values[nid] = np.asarray(params[nid], dtype=np.float64)
            else:
                xs = [values[r] for r in n["inputs"]]
                saved_inputs[nid] = tuple(x.copy() for x in xs)
                if op == "linear":
                    y = xs[0] @ xs[1]
                    if len(xs) == 3:
                        y = y + xs[2]
                elif op == "add":
                    y = xs[0] + xs[1]
                elif op == "mul":
                    y = xs[0] * xs[1]
                elif op == "relu":
                    y = np.maximum(xs[0], 0.0)
                elif op == "dropout":
                    y = xs[0] * masks[nid]
                elif op == "external":
                    y = xs[0].copy()
                elif op == "reduce_sum":
                    y = xs[0].sum(keepdims=True)
                else:
                    raise ValueError(f"oracle cannot eval op {op!r}")
                values[nid] = y
        loss = float(values[self.target].reshape(()).item())
        return loss, values, saved_inputs

    # -- backward ----------------------------------------------------------

    def backward(self, params: Mapping[str, np.ndarray],
                 inputs: Mapping[str, np.ndarray],
                 masks: Mapping[str, np.ndarray]
                 ) -> Tuple[Dict[str, np.ndarray], Dict[str, np.ndarray], float]:
        loss, values, saved_inputs = self.forward(params, inputs, masks)
        grad: Dict[str, np.ndarray] = {
            self.target: np.ones_like(values[self.target])
        }
        param_grads: Dict[str, np.ndarray] = {}
        input_grads: Dict[str, np.ndarray] = {}
        for nid in reversed(self.order):
            n = self.nodes[nid]
            op = n["op"]
            if op in ROOT_OPS:
                continue
            gout = grad.pop(nid)
            xs = saved_inputs[nid]
            if op == "linear":
                gs = [gout @ xs[1].T, xs[0].T @ gout]
                if len(xs) == 3:
                    gs.append(gout.sum(axis=0))
            elif op == "add":
                gs = [gout, gout]
            elif op == "mul":
                gs = [gout * xs[1], gout * xs[0]]
            elif op == "relu":
                gs = [gout * (xs[0] > 0)]
            elif op == "dropout":
                gs = [gout * masks[nid]]
            elif op == "external":
                gs = [gout]
            elif op == "reduce_sum":
                gs = [np.broadcast_to(gout, xs[0].shape).astype(
                    np.float64, copy=True)]
            else:
                raise ValueError(f"oracle cannot diff op {op!r}")
            for ref, gref in zip(n["inputs"], gs):
                rop = self.nodes[ref]["op"]
                if rop == "parameter":
                    param_grads[ref] = (
                        param_grads[ref] + gref if ref in param_grads else gref
                    )
                elif rop == "input":
                    input_grads[ref] = (
                        input_grads[ref] + gref if ref in input_grads else gref
                    )
                else:
                    grad[ref] = grad[ref] + gref if ref in grad else gref
        return param_grads, input_grads, loss
