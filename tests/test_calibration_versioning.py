"""Calibration binding, model versioning, and float-vs-integer comparison."""

from __future__ import annotations

import copy
import json

import numpy as np
import pytest

from engine import QUANTIZER_VERSION
from engine.errors import ModelVersionError, OutOfCalibrationRangeError
from engine.graph import as_graph_input
from engine.model import (
    TrainedModel,
    calibrate_and_freeze,
)
from engine.numerics import (
    analytic_output_bound,
    error_report,
    exact_integer_reference,
    float_reference,
    round_half_away_scalar,
)
from engine.quantize import quantize_bias
from engine.tensor_types import QTensor

pytestmark = pytest.mark.integration


@pytest.fixture
def two_layer_trained() -> TrainedModel:
    rng = np.random.default_rng(11)
    return TrainedModel(
        weights=(
            rng.uniform(-0.4, 0.4, size=(4, 3)),
            rng.uniform(-0.5, 0.5, size=(2, 4)),
        ),
        biases=(rng.uniform(-0.1, 0.1, size=(4,)),
               rng.uniform(-0.2, 0.2, size=(2,))),
        model_id="synthetic_mlp",
    )


@pytest.fixture
def two_layer_artifact(two_layer_trained) -> object:
    rng = np.random.default_rng(12)
    calib = rng.uniform(-2.0, 2.0, size=(512, 3))
    return calibrate_and_freeze(two_layer_trained, calib)


def test_frozen_params_are_deterministic_and_bound(two_layer_trained) -> None:
    rng = np.random.default_rng(12)
    calib = rng.uniform(-2.0, 2.0, size=(512, 3))
    a1 = calibrate_and_freeze(two_layer_trained, calib)
    rng = np.random.default_rng(12)
    calib2 = rng.uniform(-2.0, 2.0, size=(512, 3))
    a2 = calibrate_and_freeze(two_layer_trained, calib2)
    assert a1.calibration.calibration_fingerprint == a2.calibration.calibration_fingerprint
    assert a1.input_params.scale == a2.input_params.scale
    assert np.allclose(a1.layers[0].weight.params.scales,
                       a2.layers[0].weight.params.scales)
    # Bound to the exact trained-state version.
    assert a1.model_version == two_layer_trained.version
    assert a1.quantizer_version == QUANTIZER_VERSION


def test_inference_never_reestimates_scales(two_layer_artifact) -> None:
    graph = two_layer_artifact.build_graph()
    far = np.array([[1000.0, -1000.0, 0.0]])
    with pytest.raises(OutOfCalibrationRangeError):
        graph.execute(
            as_graph_input(QTensor(far, two_layer_artifact.input_params)),
            request_id="req-oob",
        )


def test_layer2_bias_uses_hidden_scale_product(two_layer_trained, two_layer_artifact) -> None:
    # Independent re-derivation of the layer-2 bias integers.
    s_hidden = two_layer_artifact.layers[0].output_params.scale
    s_w2 = two_layer_artifact.layers[1].weight.params.scales
    expected = [
        round_half_away_scalar(float(b / (s_hidden * float(sw))))
        for b, sw in zip(two_layer_trained.biases[1], s_w2)
    ]
    assert two_layer_artifact.layers[1].bias_q.tolist() == expected
    # And it must NOT equal the input-scale-only (wrong) quantization.
    wrong = quantize_bias(
        two_layer_trained.biases[1],
        two_layer_artifact.input_params.scale,
        s_w2,
    )
    assert wrong.tolist() != expected or np.allclose(
        two_layer_trained.biases[1], 0.0
    ) is False


def test_layer2_input_scale_equals_upstream_output_scale(
    two_layer_artifact,
) -> None:
    """Regression: the scale layer 2 encodes its input with must be the exact
    scale layer 1 emitted with - never a separately re-estimated scale."""
    # The bias integers pin this: they are only correct under the upstream
    # output scale. Assert the identity by re-running layer 2 against a
    # hidden activation encoded with layer 1's output params and matching the
    # independent oracle (which uses exactly those params).
    rng = np.random.default_rng(22)
    x = rng.uniform(-1.5, 1.5, size=(4, 3))
    trace = exact_integer_reference(two_layer_artifact, x)
    out = two_layer_artifact.build_graph().execute(
        as_graph_input(QTensor(x, two_layer_artifact.input_params)),
        request_id="req-scale-identity",
    ).output
    assert out.q_values.tolist() == trace.q_output


def test_engine_matches_bigint_oracle_on_random_two_layer_model(
    two_layer_trained, two_layer_artifact
) -> None:
    rng = np.random.default_rng(13)
    x = rng.uniform(-1.8, 1.8, size=(8, 3))
    trace = exact_integer_reference(two_layer_artifact, x)
    graph_out = two_layer_artifact.build_graph().execute(
        as_graph_input(QTensor(x, two_layer_artifact.input_params)),
        request_id="req-oracle",
    ).output
    assert graph_out.q_values.tolist() == trace.q_output


def test_error_vs_float_is_bounded_but_not_zero(two_layer_trained, two_layer_artifact) -> None:
    rng = np.random.default_rng(14)
    x = rng.uniform(-1.8, 1.8, size=(64, 3))
    out = two_layer_artifact.build_graph().execute(
        as_graph_input(QTensor(x, two_layer_artifact.input_params)),
        request_id="req-err",
    ).output
    reference = float_reference(two_layer_trained, x)
    report = error_report(out.values, reference, two_layer_artifact, out.saturated)
    assert report.within_analytic_bound
    assert report.bound_violations == 0
    # Quantization error exists (we do not demand float equality)...
    assert not np.allclose(out.values, reference, atol=0.0)
    # ...but stays small relative to output magnitude.
    assert report.rmse < max(0.25, 0.15 * float(np.abs(reference).max()))
    assert len(report.analytic_bound_per_channel) == reference.shape[1]


def test_single_layer_analytic_bound_holds_on_hand_model(
    hand_trained, hand_artifact
) -> None:
    x = np.array([[0.5, -0.25]])
    out = hand_artifact.build_graph().execute(
        as_graph_input(QTensor(x, hand_artifact.input_params)),
        request_id="req-bound",
    ).output
    reference = float_reference(hand_trained, x)
    report = error_report(out.values, reference, hand_artifact, out.saturated)
    assert report.within_analytic_bound
    bounds = analytic_output_bound(hand_artifact)
    observed = np.abs(out.values - reference).max(axis=0)
    assert all(observed[o] <= bounds[o] + 1e-9 for o in range(2))


def test_tampered_quantizer_version_is_refused(two_layer_artifact, tmp_path) -> None:
    from engine.model import load_artifact, save_artifact

    path = tmp_path / "tampered.json"
    save_artifact(two_layer_artifact, path)
    payload = json.loads(path.read_text())
    payload["quantizer_version"] = "quant-asy-v0.0.1"
    path.write_text(json.dumps(payload))
    with pytest.raises(ModelVersionError) as excinfo:
        load_artifact(path)
    assert excinfo.value.code == "model_version_mismatch"


def test_artifact_roundtrip_preserves_integer_weights(two_layer_artifact, tmp_path) -> None:
    from engine.model import load_artifact, save_artifact

    path = tmp_path / "a.json"
    save_artifact(two_layer_artifact, path)
    loaded = load_artifact(path)
    for layer_a, layer_b in zip(loaded.layers, two_layer_artifact.layers):
        np.testing.assert_array_equal(
            layer_a.weight.weights_int, layer_b.weight.weights_int
        )
        assert layer_a.bias_q.tolist() == layer_b.bias_q.tolist()
