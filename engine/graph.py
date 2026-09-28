"""Computation graph.

The graph is a small DAG of explicitly quantized ops:

    input (per-tensor QTensor)
      -> LinearNode  (integer MAC + per-channel requantize)
      -> ReLUNode    (clamp in the *integer* domain at zero_point)
      -> output      (dequantized real values)

Every node validates its inputs, performs one quantized operation, and
reports saturation counts. The executor threads a request id through every
node so a failing reproduction pinpoints the node and the key numeric state
(shapes / bounds, never raw tensor payloads).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np

from .errors import GraphExecutionError, QuantEngineError
from .kernels import IntegerMatMulResult, integer_linear
from .quantize import (
    dequantize_activations_per_channel,
    dequantize_tensor,
)
from .tensor_types import (
    PerChannelQParams,
    QParams,
    QTensor,
    QWeightMatrix,
)


@dataclass(frozen=True)
class NodeDiagnostic:
    """Per-node, per-request key state. Contains no tensor payloads."""

    node: str
    op: str
    input_shape: tuple[int, ...]
    output_shape: tuple[int, ...]
    saturated_elements: int
    total_elements: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "op": self.op,
            "input_shape": list(self.input_shape),
            "output_shape": list(self.output_shape),
            "saturated_elements": self.saturated_elements,
            "total_elements": self.total_elements,
        }


@dataclass(frozen=True)
class GraphActivation:
    """An activation flowing between quantized nodes.

    ``values`` is the dequantized float view; ``q_params`` the parameters that
    produced ``q_values``. Nodes operating in the integer domain (ReLU) use
    ``q_values``; the linear node re-encodes ``values`` itself.
    """

    values: np.ndarray
    q_params: QParams | PerChannelQParams
    q_values: np.ndarray | None = None
    saturated: np.ndarray | None = None


class GraphNode:
    op: ClassVar[str] = "node"

    def __init__(self, name: str, input_name: str, output_name: str) -> None:
        self.name = name
        self.input_name = input_name
        self.output_name = output_name

    def execute(
        self, context: dict[str, GraphActivation], *, request_id: str
    ) -> tuple[GraphActivation, NodeDiagnostic]:
        raise NotImplementedError

    def _require_input(self, context: dict[str, GraphActivation]) -> GraphActivation:
        try:
            return context[self.input_name]
        except KeyError:
            raise GraphExecutionError(
                f"node {self.name!r} references missing activation "
                f"{self.input_name!r}",
                request_node=self.name,
                missing_activation=self.input_name,
            )


class LinearNode(GraphNode):
    """Integer linear op bound to a frozen model layer."""

    op: ClassVar[str] = "linear"

    def __init__(
        self,
        name: str,
        input_name: str,
        output_name: str,
        *,
        weight: QWeightMatrix,
        bias_q: np.ndarray,
        output_params: QParams | PerChannelQParams,
        accumulator_dtype: Any,
    ) -> None:
        super().__init__(name, input_name, output_name)
        self.weight = weight
        self.bias_q = np.asarray(bias_q, dtype=np.int64)
        self.output_params = output_params
        self.accumulator_dtype = np.dtype(accumulator_dtype)

    def execute(
        self,
        context: dict[str, GraphActivation],
        *,
        request_id: str,
        enforce_range: bool = True,
    ) -> tuple[GraphActivation, NodeDiagnostic]:
        activation = self._require_input(context)
        if not isinstance(activation.q_params, QParams):
            raise GraphExecutionError(
                f"linear node {self.name!r} expects a per-tensor quantized input",
                request_id=request_id,
                request_node=self.name,
            )
        q_input = QTensor(activation.values, activation.q_params)
        result: IntegerMatMulResult = integer_linear(
            q_input,
            self.weight,
            self.output_params,
            bias_q=self.bias_q,
            accumulator_dtype=self.accumulator_dtype,
            enforce_calibration_range=enforce_range,
        )
        if isinstance(self.output_params, PerChannelQParams):
            values = dequantize_activations_per_channel(
                result.q_out, self.output_params
            )
        else:
            values = dequantize_tensor(result.q_out, self.output_params)
        out = GraphActivation(
            values=values,
            q_params=self.output_params,
            q_values=result.q_out,
            saturated=result.saturated,
        )
        diag = NodeDiagnostic(
            node=self.name,
            op=self.op,
            input_shape=activation.values.shape,
            output_shape=values.shape,
            saturated_elements=int(result.saturated.sum()),
            total_elements=int(result.saturated.size),
        )
        return out, diag


class ReLUNode(GraphNode):
    """ReLU in the integer domain: ``q = max(q, zero_point)``.

    Clipping at the encoded zero (per channel where applicable) makes the
    dequantized result clamp exactly at real 0 - no float sign or rounding.
    """

    op: ClassVar[str] = "relu"

    def execute(
        self, context: dict[str, GraphActivation], *, request_id: str
    ) -> tuple[GraphActivation, NodeDiagnostic]:
        activation = self._require_input(context)
        if activation.q_values is None:
            raise GraphExecutionError(
                f"relu node {self.name!r} requires an integer-encoded input",
                request_id=request_id,
                request_node=self.name,
            )
        params = activation.q_params
        if isinstance(params, PerChannelQParams):
            floors = params.zero_points[None, :]
            scales = params.scales[None, :]
        elif isinstance(params, QParams):
            floors = np.int64(params.zero_point)
            scales = float(params.scale)
        else:  # pragma: no cover - guarded by construction
            raise GraphExecutionError(
                f"relu node {self.name!r} got activation without q_params",
                request_id=request_id,
                request_node=self.name,
            )
        q = activation.q_values.astype(np.int64)
        q_relu = np.maximum(q, floors)
        values = scales * (q_relu - floors)
        out = GraphActivation(
            values=values,
            q_params=params,
            q_values=q_relu.astype(activation.q_values.dtype),
            saturated=np.zeros_like(activation.q_values, dtype=bool),
        )
        diag = NodeDiagnostic(
            node=self.name,
            op=self.op,
            input_shape=activation.values.shape,
            output_shape=values.shape,
            saturated_elements=0,
            total_elements=int(values.size),
        )
        return out, diag


class Graph:
    """Ordered sequence of nodes (this backend's models are sequential)."""

    def __init__(
        self,
        nodes: list[GraphNode],
        *,
        input_name: str = "input",
        output_name: str = "output",
    ) -> None:
        if not nodes:
            raise GraphExecutionError("graph must contain at least one node")
        names = [n.name for n in nodes]
        if len(set(names)) != len(names):
            raise GraphExecutionError("duplicate node names", names=names)
        self.nodes = list(nodes)
        self.input_name = input_name
        self.output_name = output_name

    @property
    def output_node(self) -> GraphNode:
        return self.nodes[-1]

    def execute(
        self,
        input_activation: GraphActivation,
        *,
        request_id: str,
        enforce_range: bool = True,
    ) -> "GraphExecutionResult":
        context: dict[str, GraphActivation] = {self.input_name: input_activation}
        diagnostics: list[NodeDiagnostic] = []
        for node in self.nodes:
            try:
                if isinstance(node, LinearNode):
                    out, diag = node.execute(
                        context,
                        request_id=request_id,
                        enforce_range=enforce_range,
                    )
                else:
                    out, diag = node.execute(context, request_id=request_id)
            except QuantEngineError as exc:
                # Typed REJECT/INDETERMINATE failures (overflow, out of
                # calibration range, invalid tensors) keep their category and
                # code; do not relabel a deterministic rejection as a 500.
                # Attach reproduction coordinates before propagating.
                exc.context.setdefault("request_id", request_id)
                exc.context.setdefault("request_node", node.name)
                raise
            except Exception as exc:  # map unexpected failures explicitly
                raise GraphExecutionError(
                    f"node {node.name!r} failed: {type(exc).__name__}: {exc}",
                    request_id=request_id,
                    request_node=node.name,
                ) from exc
            context[node.output_name] = out
            diagnostics.append(diag)
        final = context.get(self.output_node.output_name)
        return GraphExecutionResult(
            output=final,
            diagnostics=tuple(diagnostics),
            request_id=request_id,
        )


@dataclass(frozen=True)
class GraphExecutionResult:
    output: GraphActivation
    diagnostics: tuple[NodeDiagnostic, ...]
    request_id: str

    @property
    def saturated_total(self) -> int:
        return sum(d.saturated_elements for d in self.diagnostics)

    @property
    def final_layer_saturated(self) -> int:
        """Saturation in the last linear node (explicit output clipping)."""
        linear_diags = [d for d in self.diagnostics if d.op == "linear"]
        return linear_diags[-1].saturated_elements if linear_diags else 0

    @property
    def hidden_saturated(self) -> bool:
        """Whether any non-final linear node saturated.

        Hidden saturation clips an activation before it propagates, and that
        clipping error is not covered by the per-layer analytic bound. Such a
        result is reported INDETERMINATE rather than falsely certified.
        """
        linear_diags = [d for d in self.diagnostics if d.op == "linear"]
        return any(d.saturated_elements > 0 for d in linear_diags[:-1])

    def to_diagnostics_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "saturated_elements": self.saturated_total,
            "nodes": [d.to_dict() for d in self.diagnostics],
        }


def as_graph_input(q_tensor: QTensor) -> GraphActivation:
    """Wrap a request QTensor as the graph entry activation."""
    encoded = q_tensor.quantized()
    return GraphActivation(
        values=q_tensor.values,
        q_params=q_tensor.params,
        q_values=encoded,
        saturated=np.zeros_like(encoded, dtype=bool),
    )
