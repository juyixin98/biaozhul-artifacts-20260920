"""Edge cases: extremes, saturation, nonzero zero points, per-channel scales.

Each test asserts a concrete integer outcome and, where a failure is
expected, the exact failure category (REJECT) and error code.
"""

from __future__ import annotations

import numpy as np
import pytest

from engine.errors import (
    AccumulatorOverflowError,
    DecisionCategory,
    OutOfCalibrationRangeError,
    QuantizationParameterError,
)
from engine.kernels import integer_linear, static_accumulator_bounds
from engine.quantize import (
    calibrate_activation,
    quantize_tensor,
    round_half_away,
)
from engine.tensor_types import (
    PerChannelQParams,
    QParams,
    QTensor,
    QWeightMatrix,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Saturation is explicit, not host-language wraparound.
# ---------------------------------------------------------------------------


def test_quantize_saturates_at_int8_extremes() -> None:
    params = QParams(scale=1.0, zero_point=0, dtype=np.dtype("int8"))
    q = quantize_tensor(np.array([[500.0, -500.0, 127.0, -128.0]]), params)
    np.testing.assert_array_equal(q, np.array([[127, -128, 127, -128]], np.int8))


def test_kernel_reports_exact_saturation_mask_and_clips_output() -> None:
    # Input and weights chosen so one channel saturates high, the other low.
    in_params = QParams(scale=1.0, zero_point=0, dtype=np.dtype("int8"))
    w_params = PerChannelQParams(
        scales=np.array([1.0, 1.0]),
        zero_points=np.array([0, 0], dtype=np.int64),
        dtype=np.dtype("int8"),
    )
    w = QWeightMatrix(
        weights_int=np.array([[127, 127], [-128, -128]], np.int8),
        params=w_params,
    )
    out_params = PerChannelQParams(
        scales=np.array([1.0, 1.0]),
        zero_points=np.array([0, 0], dtype=np.int64),
        dtype=np.dtype("int8"),
    )
    x = np.array([[127.0, 127.0]])  # channel0 mac huge positive; ch1 huge negative
    result = integer_linear(
        x,
        w,
        out_params,
        input_params=in_params,
        accumulator_dtype=np.dtype("int64"),
        enforce_calibration_range=False,
    )
    np.testing.assert_array_equal(result.q_out, np.array([[127, -128]], np.int8))
    assert result.saturated.tolist() == [[True, True]]


# ---------------------------------------------------------------------------
# Nonzero zero point: 0 real maps to a nonzero integer and back exactly.
# ---------------------------------------------------------------------------


def test_nonzero_zero_point_roundtrips_real_zero_exactly() -> None:
    params = QParams(scale=0.25, zero_point=42, dtype=np.dtype("int8"))
    q = quantize_tensor(np.array([[0.0]]), params)
    assert q.tolist() == [[42]]
    from engine.quantize import dequantize_tensor

    assert dequantize_tensor(q, params).tolist() == [[0.0]]


def test_zero_point_correction_handles_negative_inputs_with_unsigned_like_zp() -> None:
    # zp inside the range so negative reals are encoded below zp (asymmetric).
    params = calibrate_activation(np.array([[-3.0, 1.0]]), np.dtype("int8"))
    assert params.zero_point != 0
    qx = quantize_tensor(np.array([[-3.0, 1.0]]), params)
    # De-zero-pointed deviations must carry the correct sign.
    deviations = qx.astype(np.int64) - params.zero_point
    assert deviations[0, 0] < 0 < deviations[0, 1]


# ---------------------------------------------------------------------------
# Distinct per-channel scales give distinct results on identical products.
# ---------------------------------------------------------------------------


def test_per_channel_scales_differentiate_identical_integer_products() -> None:
    in_params = QParams(scale=1.0, zero_point=0, dtype=np.dtype("int8"))
    # Two channels with the SAME integer weights but 5x different scales.
    w_params = PerChannelQParams(
        scales=np.array([1.0, 0.2]),
        zero_points=np.array([0, 0], dtype=np.int64),
        dtype=np.dtype("int8"),
    )
    w = QWeightMatrix(
        weights_int=np.array([[10, 0], [10, 0]], np.int8), params=w_params
    )
    out_params = PerChannelQParams(
        scales=np.array([1.0, 1.0]),
        zero_points=np.array([0, 0], dtype=np.int64),
        dtype=np.dtype("int8"),
    )
    result = integer_linear(
        np.array([[2.0, 0.0]]),
        w,
        out_params,
        input_params=in_params,
        accumulator_dtype=np.dtype("int32"),
    )
    # mac identical (20); multiplier differs 5x -> [20, 4].
    assert result.q_out.tolist() == [[20, 4]]


# ---------------------------------------------------------------------------
# Accumulator overflow is rejected categorically, never wrapped.
# ---------------------------------------------------------------------------


def test_int32_accumulator_overflow_is_rejected_before_execution() -> None:
    in_params = QParams(scale=1.0, zero_point=0, dtype=np.dtype("int8"))
    w_params = PerChannelQParams(
        scales=np.array([1.0]),
        zero_points=np.array([0], dtype=np.int64),
        dtype=np.dtype("int8"),
    )
    k_dim = 200_000  # K*127*127 ~= 3.23e9 > int32 max 2.15e9
    w = QWeightMatrix(
        weights_int=np.full((1, k_dim), 127, dtype=np.int8), params=w_params
    )
    out_params = PerChannelQParams(
        scales=np.array([1.0]),
        zero_points=np.array([0], dtype=np.int64),
        dtype=np.dtype("int8"),
    )
    mac_min, mac_max, lim_min, lim_max = static_accumulator_bounds(
        k_dim, in_params, w, np.dtype("int32")
    )
    assert mac_max > lim_max
    with pytest.raises(AccumulatorOverflowError) as excinfo:
        integer_linear(
            np.ones((1, k_dim)),
            w,
            out_params,
            input_params=in_params,
            accumulator_dtype=np.dtype("int32"),
            enforce_calibration_range=False,
        )
    assert excinfo.value.category is DecisionCategory.REJECT
    assert excinfo.value.code == "accumulator_overflow"
    # The same op must be accepted when a wider accumulator is declared.
    ok = integer_linear(
        np.ones((1, k_dim)),
        w,
        out_params,
        input_params=in_params,
        accumulator_dtype=np.dtype("int64"),
        enforce_calibration_range=False,
    )
    assert ok.q_out.shape == (1, 1)


# ---------------------------------------------------------------------------
# Out-of-calibration inputs are rejected, not silently rescaled.
# ---------------------------------------------------------------------------


def test_input_outside_calibrated_range_is_rejected() -> None:
    params = QParams(scale=0.01, zero_point=-10, dtype=np.dtype("int8"))
    x = QTensor(np.array([[5.0]]), params)
    with pytest.raises(OutOfCalibrationRangeError) as excinfo:
        quantize_tensor(x.values, params, check_range=True)
    assert excinfo.value.category is DecisionCategory.REJECT
    assert excinfo.value.code == "input_out_of_calibration_range"
    # The encoded value would saturate - that is exactly why range is enforced.
    assert quantize_tensor(x.values, params).tolist() == [[127]]


# ---------------------------------------------------------------------------
# Rounding is deterministic half-away-from-zero (no banker's rounding).
# ---------------------------------------------------------------------------


def test_round_half_away_is_deterministic_on_ties() -> None:
    vals = np.array([0.5, 1.5, 2.5, -0.5, -1.5, -2.5])
    assert round_half_away(vals).tolist() == [1, 2, 3, -1, -2, -3]
    # NumPy's default would give [0, 2, 2, 0, -2, -2] (banker's) - rejected.
    assert np.round(vals).tolist() == [0.0, 2.0, 2.0, -0.0, -2.0, -2.0]


# ---------------------------------------------------------------------------
# Parameter validation: bad scales and zero points are rejected.
# ---------------------------------------------------------------------------


def test_invalid_quantization_parameters_are_rejected() -> None:
    with pytest.raises(QuantizationParameterError):
        QParams(scale=0.0, zero_point=0, dtype=np.dtype("int8"))
    with pytest.raises(QuantizationParameterError):
        QParams(scale=1.0, zero_point=999, dtype=np.dtype("int8"))
    with pytest.raises(QuantizationParameterError):
        PerChannelQParams(
            scales=np.array([1.0, -1.0]),
            zero_points=np.array([0, 0], dtype=np.int64),
            dtype=np.dtype("int8"),
        )
