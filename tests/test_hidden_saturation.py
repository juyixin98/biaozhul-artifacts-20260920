"""Hidden-layer saturation forces an INDETERMINATE verdict.

The analytic per-layer bound covers rounding/encoding error but not clipping
in an intermediate activation. When a hidden node saturates, the service must
not certify the result - it must report INDETERMINATE with the reason and the
node coordinates. The model here is hand-constructed so saturation is
deterministic and does not depend on sampled calibration data.
"""

from __future__ import annotations

import numpy as np
import pytest

from engine.model import (
    CalibrationBinding,
    QuantizedLayerArtifact,
    QuantizedModelArtifact,
    TrainedModel,
)
from engine.numerics import error_report
from engine.graph import as_graph_input
from engine.tensor_types import (
    PerChannelQParams,
    QParams,
    QTensor,
    QWeightMatrix,
)

pytestmark = pytest.mark.integration


def _hidden_saturating_artifact() -> tuple[QuantizedModelArtifact, TrainedModel]:
    # Layer 1: two inputs, two hidden. s_x=1, zp=0; integer weights all 1.
    # Real input 127 -> mac 254 per hidden channel -> hidden int8 saturates.
    in_params = QParams(scale=1.0, zero_point=0, dtype=np.dtype("int8"))
    w1_params = PerChannelQParams(
        scales=np.array([1.0, 1.0]),
        zero_points=np.array([0, 0], dtype=np.int64),
        dtype=np.dtype("int8"),
    )
    w1 = QWeightMatrix(
        weights_int=np.array([[1, 1], [1, 1]], np.int8), params=w1_params
    )
    hidden_params = QParams(scale=1.0, zero_point=0, dtype=np.dtype("int8"))

    # Layer 2: zero weights/bias so the final output is exactly zero on both
    # paths; this isolates the hidden-saturation condition itself.
    w2_params = PerChannelQParams(
        scales=np.array([0.01, 0.01]),
        zero_points=np.array([0, 0], dtype=np.int64),
        dtype=np.dtype("int8"),
    )
    w2 = QWeightMatrix(
        weights_int=np.array([[0, 0], [0, 0]], np.int8), params=w2_params
    )
    out_params = PerChannelQParams(
        scales=np.array([1.0, 1.0]),
        zero_points=np.array([0, 0], dtype=np.int64),
        dtype=np.dtype("int8"),
    )
    trained = TrainedModel(
        weights=(
            np.array([[1.0, 1.0], [1.0, 1.0]]),
            np.zeros((2, 2)),
        ),
        biases=(np.zeros(2), np.zeros(2)),
        model_id="hidden_sat",
    )
    layers = (
        QuantizedLayerArtifact(
            name="layer1",
            weight=w1,
            bias_q=np.zeros(2, dtype=np.int64),
            output_params=hidden_params,
            apply_relu=True,
            accumulator_dtype=np.dtype("int32"),
        ),
        QuantizedLayerArtifact(
            name="layer2",
            weight=w2,
            bias_q=np.zeros(2, dtype=np.int64),
            output_params=out_params,
            apply_relu=False,
            accumulator_dtype=np.dtype("int32"),
        ),
    )
    artifact = QuantizedModelArtifact(
        model_id="hidden_sat",
        model_version=trained.version,
        quantizer_version="quant-asy-v1.0.0",
        input_params=in_params,
        layers=layers,
        calibration=CalibrationBinding(
            sample_count=1,
            created_at="2026-09-28T00:00:00+00:00",
            calibration_fingerprint="handcrafted-hidden-sat",
        ),
        architecture=(2, 2, 2),
    )
    return artifact, trained


def test_hidden_saturation_is_flagged_and_unprovable() -> None:
    artifact, trained = _hidden_saturating_artifact()
    x = np.array([[127.0, 127.0]])  # in calibrated input range [-128,127]

    result = artifact.build_graph().execute(
        as_graph_input(QTensor(x, artifact.input_params)),
        request_id="req-hidden-sat",
    )
    assert result.hidden_saturated is True
    # Hidden node really clipped: mac 254 -> 127.
    layer1_diag = result.diagnostics[0]
    assert layer1_diag.saturated_elements == 2

    from engine.numerics import float_reference

    reference = float_reference(trained, x)
    report = error_report(
        result.output.values,
        reference,
        artifact,
        result.output.saturated,
        hidden_saturation=result.hidden_saturated,
    )
    assert report.hidden_saturation is True
    assert report.within_analytic_bound is False


def test_service_returns_indeterminate_for_hidden_saturation() -> None:
    from service.inference import InferenceService
    from service.registry import ModelRegistry

    artifact, trained = _hidden_saturating_artifact()
    registry = ModelRegistry()
    registry.register(artifact, trained)
    verdict = InferenceService(registry).validate(
        "hidden_sat", [[127.0, 127.0]]
    )
    assert verdict.accepted is False
    assert verdict.reason.startswith("INDETERMINATE")
    assert "hidden" in verdict.reason
    assert verdict.error_report["hidden_saturation"] is True
    assert verdict.request_id.startswith("req-")
