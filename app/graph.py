"""Quantized computation graph and model registry.

A :class:`QuantizedLayer` owns its *frozen* integer weights, per-channel weight
params, per-tensor input params, per-channel output params and int32 bias in
the correct accumulator scale. A :class:`QuantizedModel` is an ordered
collection of such layers plus the float reference material needed by the
independent verification layer (:mod:`app.verification`).

The :class:`ModelRegistry` binds exactly one calibration fingerprint to one
``(model_id, version)`` and rejects mismatched versions at request time.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Mapping

import numpy as np

from .calibration import CalibrationBundle, TensorParams
from .kernel import MatMulResult, quantize_bias, quantized_matmul
from .tensor_types import (
    QuantTensor,
    quantize_per_channel,
    quantize_per_tensor,
)
from .training_state import FrozenModelState, ModelLifecycle
from .errors import (
    InvalidInputError,
    ModelVersionMismatchError,
)


@dataclass(frozen=True)
class LayerIO:
    name: str
    integer_result: MatMulResult
    output_float: np.ndarray  # dequantized layer output (float32)


class QuantizedLayer:
    """One frozen integer fully-connected layer."""

    def __init__(
        self,
        name: str,
        weight: QuantTensor,
        input_spec,
        output_spec,
        bias_q: np.ndarray | None = None,
    ) -> None:
        if not weight.spec.per_channel:
            raise ValueError("layer weight must be per-channel quantized")
        self.name = name
        self.weight = weight
        self.input_spec = input_spec
        self.output_spec = output_spec
        self.bias_q = None if bias_q is None else np.asarray(bias_q, dtype=np.int32)

    @property
    def out_channels(self) -> int:
        return self.weight.spec.num_channels

    def forward(self, act: QuantTensor) -> MatMulResult:
        if act.spec.per_channel:
            raise InvalidInputError(f"layer {self.name!r}: input must be per-tensor quantized")
        return quantized_matmul(
            act, self.weight, out_spec=self.output_spec, bias_q=self.bias_q
        )

    def quantize_input(self, x: np.ndarray) -> QuantTensor:
        """Quantize a float batch with the *bound* input calibration."""
        return quantize_per_tensor(x, float(self.input_spec.scale), int(self.input_spec.zero_point))


def build_layer_from_float(
    name: str,
    weight_float: np.ndarray,
    bias_float: np.ndarray | None,
    params: Mapping[str, TensorParams],
) -> QuantizedLayer:
    """Materialize a frozen layer from float weights + calibration params."""
    for role in ("input", "weight", "output"):
        if role not in params:
            raise ValueError(f"layer {name!r}: calibration missing role {role!r}")
    input_spec = params["input"].spec()
    weight_spec = params["weight"].spec()
    output_spec = params["output"].spec()

    w_float = np.asarray(weight_float, dtype=np.float32)
    weight_q = quantize_per_channel(w_float, weight_spec.scale, weight_spec.zero_point, axis=0)
    bias_q = None
    if bias_float is not None:
        bias_q = quantize_bias(np.asarray(bias_float, dtype=np.float32), input_spec, weight_spec)
    return QuantizedLayer(name, weight_q, input_spec, output_spec, bias_q)


class QuantizedModel:
    """Ordered stack of frozen quantized layers + float reference material."""

    def __init__(
        self,
        model_id: str,
        model_version: str,
        layers: OrderedDict[str, QuantizedLayer],
        *,
        float_weights: Mapping[str, np.ndarray],
        float_biases: Mapping[str, np.ndarray | None],
    ) -> None:
        if not layers:
            raise ValueError("model needs at least one layer")
        self.model_id = model_id
        self.model_version = model_version
        self.layers = layers
        # Defensive copies: reference material must never alias caller arrays.
        self._float_weights = {k: np.array(v, dtype=np.float32, copy=True)
                               for k, v in float_weights.items()}
        self._float_biases = {
            k: (None if v is None else np.array(v, dtype=np.float32, copy=True))
            for k, v in float_biases.items()
        }

    @property
    def float_weights(self) -> dict[str, np.ndarray]:
        return {k: v.copy() for k, v in self._float_weights.items()}

    @property
    def float_biases(self) -> dict[str, np.ndarray | None]:
        return {k: (None if v is None else v.copy()) for k, v in self._float_biases.items()}

    def layer_order(self) -> list[str]:
        return list(self.layers.keys())

    def execute_float(self, x: np.ndarray) -> "OrderedDict[str, np.ndarray]":
        """Run the integer graph on a float batch, quantizing with bound specs.

        Multi-layer models cross layers through an explicit requantization
        bridge (dequant → requantize with the *next* layer's bound input
        spec); no statistics are estimated at request time.
        """
        x = np.asarray(x, dtype=np.float32)
        if x.ndim != 2:
            raise InvalidInputError(f"input must be 2-D (M, K), got shape {x.shape}")
        if not np.all(np.isfinite(x)):
            raise InvalidInputError("input contains non-finite values")

        trace: OrderedDict[str, LayerIO] = OrderedDict()
        current_float = x
        for name, layer in self.layers.items():
            act = layer.quantize_input(current_float)
            result = layer.forward(act)
            out_float = result.output.dequantize()
            trace[name] = LayerIO(name=name, integer_result=result, output_float=out_float)
            current_float = out_float
        return trace


def build_model_from_float(
    model_id: str,
    model_version: str,
    weights: Mapping[str, np.ndarray],
    biases: Mapping[str, np.ndarray | None],
    bundle: CalibrationBundle,
) -> QuantizedModel:
    """Compile float weights + a frozen bundle into a deployable model."""
    layers: OrderedDict[str, QuantizedLayer] = OrderedDict()
    for name in bundle.layers:  # bundle order defines execution order
        if name not in weights:
            raise ValueError(f"calibration has layer {name!r} but no weights were supplied")
        layers[name] = build_layer_from_float(name, weights[name], biases.get(name),
                                              bundle.layer(name))
    return QuantizedModel(model_id, model_version, layers,
                          float_weights=weights, float_biases=biases)


@dataclass(frozen=True)
class RegisteredModel:
    model: QuantizedModel
    state: FrozenModelState
    bundle: CalibrationBundle


class ModelRegistry:
    """In-process registry; one frozen calibration per (model_id, version)."""

    def __init__(self) -> None:
        self._models: dict[str, RegisteredModel] = {}

    def register(
        self,
        model: QuantizedModel,
        bundle: CalibrationBundle,
        lifecycle: ModelLifecycle,
    ) -> None:
        state = lifecycle.frozen
        if not state.matches(model.model_id, model.model_version):
            raise ValueError("lifecycle state does not match model identity")
        if model.model_id in self._models:
            existing = self._models[model.model_id].state
            raise ValueError(
                f"model {model.model_id} already registered as version "
                f"{existing.model_version}; versions are immutable"
            )
        self._models[model.model_id] = RegisteredModel(model=model, state=state, bundle=bundle)

    def get(self, model_id: str, model_version: str | None = None) -> RegisteredModel:
        entry = self._models.get(model_id)
        if entry is None:
            raise InvalidInputError(
                f"unknown model_id {model_id!r}",
                details={"known_models": sorted(self._models)},
            )
        if model_version is not None and entry.state.model_version != model_version:
            raise ModelVersionMismatchError(
                f"model {model_id!r} is deployed at version "
                f"{entry.state.model_version!r}, not {model_version!r}",
                details={
                    "requested_version": model_version,
                    "deployed_version": entry.state.model_version,
                    "calibration_fingerprint": entry.state.calibration_fingerprint,
                },
            )
        return entry

    def ids(self) -> list[str]:
        return sorted(self._models)
