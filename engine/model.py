"""Trained float state and frozen quantized model artifacts.

Two distinct responsibilities live here:

* :class:`TrainedModel` - the *training state*: float weights/biases exactly
  as produced by training, with a content checksum used as the model version.
* :class:`QuantizedModelArtifact` - the frozen inference artifact: integer
  weights, per-channel scales, zero points, quantized biases, the calibration
  binding and the quantizer-semantics version. Calibration parameters are
  estimated exactly once here and never touched again at request time.

Loading an artifact whose ``quantizer_version`` does not match this engine is
refused (REJECT) - an artifact produced under different rounding/zero-point
semantics must not silently run on new semantics.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from . import QUANTIZER_VERSION
from .errors import (
    ModelNotFoundError,
    ModelVersionError,
    QuantizationParameterError,
)
from .graph import (
    Graph,
    GraphNode,
    LinearNode,
    ReLUNode,
)
from .kernels import DEFAULT_ACCUMULATOR_DTYPE
from .quantize import (
    calibrate_activation,
    calibrate_output_channel,
    calibrate_weights,
    quantize_bias,
    quantize_weights_per_channel,
)
from .tensor_types import (
    PerChannelQParams,
    QParams,
    QWeightMatrix,
)


# ---------------------------------------------------------------------------
# Trained (float) state.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrainedModel:
    """Float training state of a sequential MLP.

    Layer ``i`` computes ``y = x @ W[i].T + b[i]``; all but the final layer
    are followed by ReLU.
    """

    weights: tuple[np.ndarray, ...]
    biases: tuple[np.ndarray, ...]
    model_id: str

    def __post_init__(self) -> None:
        if len(self.weights) != len(self.biases) or not self.weights:
            raise QuantizationParameterError(
                "weights and biases must be non-empty and equal in count",
                weight_count=len(self.weights),
                bias_count=len(self.biases),
            )
        for i, (w, b) in enumerate(zip(self.weights, self.biases)):
            w = np.asarray(w, dtype=np.float64)
            b = np.asarray(b, dtype=np.float64)
            if w.ndim != 2 or b.ndim != 1 or w.shape[0] != b.shape[0]:
                raise QuantizationParameterError(
                    "layer shape mismatch",
                    layer=i,
                    weight_shape=tuple(w.shape),
                    bias_shape=tuple(b.shape),
                )
            if i > 0 and w.shape[1] != self.weights[i - 1].shape[0]:
                raise QuantizationParameterError(
                    "consecutive layers do not connect",
                    layer=i,
                    weight_shape=tuple(w.shape),
                    previous_out=int(self.weights[i - 1].shape[0]),
                )
        object.__setattr__(
            self,
            "weights",
            tuple(np.asarray(w, dtype=np.float64) for w in self.weights),
        )
        object.__setattr__(
            self,
            "biases",
            tuple(np.asarray(b, dtype=np.float64) for b in self.biases),
        )

    @property
    def architecture(self) -> tuple[int, ...]:
        dims = [self.weights[0].shape[1]]
        dims.extend(w.shape[0] for w in self.weights)
        return tuple(dims)

    @property
    def version(self) -> str:
        """Content checksum of the float state - the model version."""
        h = hashlib.sha256()
        h.update(self.model_id.encode("utf-8"))
        for w in self.weights:
            h.update(np.ascontiguousarray(w).tobytes())
        for b in self.biases:
            h.update(np.ascontiguousarray(b).tobytes())
        return h.hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "architecture": list(self.architecture),
            "weights": [w.tolist() for w in self.weights],
            "biases": [b.tolist() for b in self.biases],
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrainedModel":
        model = cls(
            weights=tuple(np.asarray(w, dtype=np.float64) for w in data["weights"]),
            biases=tuple(np.asarray(b, dtype=np.float64) for b in data["biases"]),
            model_id=str(data["model_id"]),
        )
        stored = data.get("version")
        if stored is not None and stored != model.version:
            raise ModelVersionError(
                "trained state checksum does not match stored version",
                stored_version=str(stored),
                computed_version=model.version,
            )
        return model


def save_trained_model(model: TrainedModel, path: str | Path) -> None:
    Path(path).write_text(json.dumps(model.to_dict(), indent=2), encoding="utf-8")


def load_trained_model(path: str | Path) -> TrainedModel:
    return TrainedModel.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Frozen quantized artifact.
# ---------------------------------------------------------------------------


def _qparams_to_dict(params: QParams | PerChannelQParams) -> dict[str, Any]:
    return {"kind": type(params).__name__, **params.to_dict()}


def _qparams_from_dict(data: dict[str, Any]) -> QParams | PerChannelQParams:
    kind = data["kind"]
    payload = {k: v for k, v in data.items() if k != "kind"}
    if kind == "QParams":
        return QParams.from_dict(payload)
    if kind == "PerChannelQParams":
        return PerChannelQParams.from_dict(payload)
    raise ModelVersionError("unknown quantization parameter kind", kind=kind)


@dataclass(frozen=True)
class QuantizedLayerArtifact:
    name: str
    weight: QWeightMatrix
    bias_q: np.ndarray
    output_params: QParams | PerChannelQParams
    apply_relu: bool
    accumulator_dtype: np.dtype

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "weights_int": self.weight.weights_int.tolist(),
            "weight_params": _qparams_to_dict(self.weight.params),
            "bias_q": [int(v) for v in self.bias_q],
            "output_params": _qparams_to_dict(self.output_params),
            "apply_relu": self.apply_relu,
            "accumulator_dtype": self.accumulator_dtype.name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QuantizedLayerArtifact":
        wparams_data = data["weight_params"]
        wparams = _qparams_from_dict(wparams_data)
        assert isinstance(wparams, PerChannelQParams)
        return cls(
            name=str(data["name"]),
            weight=QWeightMatrix(
                weights_int=np.asarray(data["weights_int"], dtype=wparams.dtype),
                params=wparams,
            ),
            bias_q=np.asarray(data["bias_q"], dtype=np.int64),
            output_params=_qparams_from_dict(data["output_params"]),
            apply_relu=bool(data["apply_relu"]),
            accumulator_dtype=np.dtype(str(data["accumulator_dtype"])),
        )

    def build_nodes(self, input_name: str, index: int) -> tuple[list[GraphNode], str]:
        linear_out = f"{self.name}_out"
        nodes: list[GraphNode] = [
            LinearNode(
                name=f"{self.name}.linear",
                input_name=input_name,
                output_name=linear_out,
                weight=self.weight,
                bias_q=self.bias_q,
                output_params=self.output_params,
                accumulator_dtype=self.accumulator_dtype,
            )
        ]
        final_name = linear_out
        if self.apply_relu:
            relu_out = f"{self.name}_relu"
            nodes.append(
                ReLUNode(
                    name=f"{self.name}.relu",
                    input_name=linear_out,
                    output_name=relu_out,
                )
            )
            final_name = relu_out
        return nodes, final_name


@dataclass(frozen=True)
class CalibrationBinding:
    """Provenance of the frozen calibration."""

    sample_count: int
    created_at: str
    calibration_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_count": self.sample_count,
            "created_at": self.created_at,
            "calibration_fingerprint": self.calibration_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CalibrationBinding":
        return cls(
            sample_count=int(data["sample_count"]),
            created_at=str(data["created_at"]),
            calibration_fingerprint=str(data["calibration_fingerprint"]),
        )


@dataclass(frozen=True)
class QuantizedModelArtifact:
    """Frozen inference artifact. Immutable; safe to share across requests."""

    model_id: str
    model_version: str
    quantizer_version: str
    input_params: QParams
    layers: tuple[QuantizedLayerArtifact, ...]
    calibration: CalibrationBinding
    architecture: tuple[int, ...]

    def build_graph(self) -> Graph:
        nodes: list[GraphNode] = []
        current = "input"
        for i, layer in enumerate(self.layers):
            layer_nodes, current = layer.build_nodes(current, i)
            nodes.extend(layer_nodes)
        return Graph(nodes=nodes, input_name="input", output_name=current)

    @property
    def output_params(self) -> QParams | PerChannelQParams:
        return self.layers[-1].output_params

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "quantizer_version": self.quantizer_version,
            "architecture": list(self.architecture),
            "input_params": _qparams_to_dict(self.input_params),
            "layers": [layer.to_dict() for layer in self.layers],
            "calibration": self.calibration.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QuantizedModelArtifact":
        quantizer_version = str(data["quantizer_version"])
        if quantizer_version != QUANTIZER_VERSION:
            raise ModelVersionError(
                "artifact quantization semantics do not match this engine; "
                "recalibrate rather than reusing stale parameters",
                artifact_quantizer_version=quantizer_version,
                engine_quantizer_version=QUANTIZER_VERSION,
            )
        input_params = _qparams_from_dict(data["input_params"])
        assert isinstance(input_params, QParams)
        return cls(
            model_id=str(data["model_id"]),
            model_version=str(data["model_version"]),
            quantizer_version=quantizer_version,
            input_params=input_params,
            layers=tuple(
                QuantizedLayerArtifact.from_dict(layer) for layer in data["layers"]
            ),
            calibration=CalibrationBinding.from_dict(data["calibration"]),
            architecture=tuple(int(d) for d in data["architecture"]),
        )


def save_artifact(artifact: QuantizedModelArtifact, path: str | Path) -> None:
    path = Path(path)
    path.write_text(json.dumps(artifact.to_dict(), indent=2), encoding="utf-8")


def load_artifact(path: str | Path) -> QuantizedModelArtifact:
    path = Path(path)
    if not path.exists():
        raise ModelNotFoundError("model artifact file not found", path=str(path))
    return QuantizedModelArtifact.from_dict(
        json.loads(path.read_text(encoding="utf-8"))
    )


# ---------------------------------------------------------------------------
# Calibration: trained state + fixed calibration set -> frozen artifact.
# ---------------------------------------------------------------------------


def _fingerprint(samples: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(samples).tobytes()).hexdigest()[:16]


def calibrate_and_freeze(
    trained: TrainedModel,
    calibration_inputs: np.ndarray,
    *,
    activation_dtype: Any = np.dtype("int8"),
    weight_dtype: Any = np.dtype("int8"),
    output_dtype: Any = np.dtype("int8"),
    accumulator_dtype: Any = DEFAULT_ACCUMULATOR_DTYPE,
    hidden_relu: bool = True,
) -> QuantizedModelArtifact:
    """Estimate every quantization parameter once and freeze the artifact.

    Sequence (matters for bias scales):

    1. per-tensor activation params from the fixed calibration inputs;
    2. per-output-channel weight params for layer 1; bias quantized at
       ``s_x * s_w1``;
    3. hidden activation range measured from *float* calibration forwards
       (per-tensor params for the intermediate activation);
    4. per-channel weight params for layer 2; bias quantized at
       ``s_hidden * s_w2`` - never ``s_x * s_w2``;
    5. per-channel output params from the float calibration outputs.
    """
    x = np.asarray(calibration_inputs, dtype=np.float64)
    if x.ndim != 2 or x.shape[1] != trained.architecture[0]:
        raise QuantizationParameterError(
            "calibration input shape mismatch",
            calibration_shape=tuple(x.shape),
            expected_features=trained.architecture[0],
        )
    if not np.all(np.isfinite(x)):
        raise QuantizationParameterError("calibration inputs must be finite")

    activation_dtype = np.dtype(activation_dtype)
    weight_dtype = np.dtype(weight_dtype)
    output_dtype = np.dtype(output_dtype)
    accumulator_dtype = np.dtype(accumulator_dtype)

    # Float calibration forwards, independent of the integer path. Both the
    # pre-activation range (used to calibrate a hidden layer's output params -
    # the linear node emits values *before* ReLU, including negatives) and the
    # post-activation values (fed to the next layer) are recorded.
    pre_acts: list[np.ndarray] = [x]
    post_acts: list[np.ndarray] = [x]
    cur = x
    for i, (w, b) in enumerate(zip(trained.weights, trained.biases)):
        z = cur @ w.T + b
        pre_acts.append(z)
        if i < len(trained.weights) - 1 and hidden_relu:
            cur = np.maximum(z, 0.0)
        else:
            cur = z
        post_acts.append(cur)

    input_params = calibrate_activation(post_acts[0], activation_dtype)

    layer_artifacts: list[QuantizedLayerArtifact] = []
    prev_out_params: QParams = input_params
    for i, (w, b) in enumerate(zip(trained.weights, trained.biases)):
        is_last = i == len(trained.weights) - 1
        wparams = calibrate_weights(w, weight_dtype)
        w_int = quantize_weights_per_channel(w, wparams)
        qweight = QWeightMatrix(weights_int=w_int, params=wparams)

        # The scale of this layer's input activation is exactly the scale the
        # upstream node encoded it with: layer 1 uses the request params,
        # layer i>0 uses layer i-1's output params (the ReLU node keeps the
        # same q_params). Re-calibrating the post-ReLU tensor with a different
        # range would silently change that scale and mis-quantize the bias.
        in_act_params = prev_out_params
        bq = quantize_bias(b, in_act_params.scale, wparams.scales)

        if is_last:
            y = pre_acts[i + 1]
            out_params: QParams | PerChannelQParams = calibrate_output_channel(
                y.min(axis=0), y.max(axis=0), output_dtype
            )
        else:
            # Pre-ReLU range: negatives must fit so only genuine out-of-range
            # requests saturate; the integer ReLU node then clips at zero.
            out_params = calibrate_activation(pre_acts[i + 1], activation_dtype)
            prev_out_params = out_params

        layer_artifacts.append(
            QuantizedLayerArtifact(
                name=f"layer{i + 1}",
                weight=qweight,
                bias_q=bq,
                output_params=out_params,
                apply_relu=(not is_last) and hidden_relu,
                accumulator_dtype=accumulator_dtype,
            )
        )

    binding = CalibrationBinding(
        sample_count=int(x.shape[0]),
        created_at=datetime.now(timezone.utc).isoformat(),
        calibration_fingerprint=_fingerprint(x),
    )
    return QuantizedModelArtifact(
        model_id=trained.model_id,
        model_version=trained.version,
        quantizer_version=QUANTIZER_VERSION,
        input_params=input_params,
        layers=tuple(layer_artifacts),
        calibration=binding,
        architecture=trained.architecture,
    )
