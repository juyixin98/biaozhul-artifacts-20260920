"""Shared fixtures.

The ``hand_artifact`` fixture is a fully hand-specified single-layer model
whose every integer result has been computed by hand (see
test_exact_handcomputed.py). It is the authoritative oracle: nothing in the
engine generated those numbers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from engine.model import (
    CalibrationBinding,
    QuantizedLayerArtifact,
    QuantizedModelArtifact,
    TrainedModel,
)
from engine.tensor_types import (
    PerChannelQParams,
    QParams,
    QWeightMatrix,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# Hand-specified quantization parameters (all int8).
INPUT_PARAMS = QParams(scale=0.25, zero_point=10, dtype=np.dtype("int8"))
WEIGHT_PARAMS = PerChannelQParams(
    scales=np.array([0.5, 0.1]),
    zero_points=np.array([-3, 20], dtype=np.int64),
    dtype=np.dtype("int8"),
)
OUTPUT_PARAMS = PerChannelQParams(
    scales=np.array([0.5, 0.25]),
    zero_points=np.array([-5, 0], dtype=np.int64),
    dtype=np.dtype("int8"),
)
WEIGHTS_INT = np.array([[4, -2], [10, 30]], dtype=np.int8)
BIAS_Q = np.array([3, -2], dtype=np.int64)
# Real request and the corresponding hand-computed expectations:
#   qx = [12, 9], deviations [2, -1]
#   mac = [13, -32 before bias? see tests] -> channel 1: 2*-10 + -1*10 = -30
#   biased = [16, -32]
#   q_out = [-1, -3], real out = [2.0, -0.75]
HAND_INPUT = np.array([[0.5, -0.25]])
EXPECTED_Q_INPUT = np.array([[12, 9]])
EXPECTED_MAC = np.array([[13, -30]])
EXPECTED_BIASED = np.array([[16, -32]])
EXPECTED_Q_OUT = np.array([[-1, -3]])
EXPECTED_REAL_OUT = np.array([[2.0, -0.75]])

# Float state consistent with the integer artifact (dequantized weights).
FLOAT_WEIGHTS = (np.array([[3.5, 0.5], [-1.0, 1.0]]),)
FLOAT_BIASES = (np.array([0.375, -0.05]),)


@pytest.fixture
def hand_trained() -> TrainedModel:
    return TrainedModel(
        weights=FLOAT_WEIGHTS,
        biases=FLOAT_BIASES,
        model_id="hand_model",
    )


def _build_artifact(*, apply_relu: bool) -> QuantizedModelArtifact:
    weight = QWeightMatrix(weights_int=WEIGHTS_INT, params=WEIGHT_PARAMS)
    layer = QuantizedLayerArtifact(
        name="layer1",
        weight=weight,
        bias_q=BIAS_Q,
        output_params=OUTPUT_PARAMS,
        apply_relu=apply_relu,
        accumulator_dtype=np.dtype("int32"),
    )
    trained = TrainedModel(
        weights=FLOAT_WEIGHTS, biases=FLOAT_BIASES, model_id="hand_model"
    )
    return QuantizedModelArtifact(
        model_id="hand_model",
        model_version=trained.version,
        quantizer_version="quant-asy-v1.0.0",
        input_params=INPUT_PARAMS,
        layers=(layer,),
        calibration=CalibrationBinding(
            sample_count=1,
            created_at="2026-09-28T00:00:00+00:00",
            calibration_fingerprint="handcrafted",
        ),
        architecture=(2, 2),
    )


@pytest.fixture
def hand_artifact() -> QuantizedModelArtifact:
    return _build_artifact(apply_relu=False)


@pytest.fixture
def hand_artifact_relu() -> QuantizedModelArtifact:
    return _build_artifact(apply_relu=True)
