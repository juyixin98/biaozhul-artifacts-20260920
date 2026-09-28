"""Calibration, (de)quantization and requantization math.

All quantization parameters are produced by *calibration* over a fixed
calibration set and then frozen into the model artifact (see ``model.py``).
Inference never re-estimates scales or zero points - a request that does not
fit the calibrated range is rejected, not silently rescaled.

Rounding is explicitly round-half-away-from-zero everywhere (NumPy's default
banker's rounding is never used for integer encoding).

Order of operations in the integer pipeline (see ``kernels.py``)::

    1. qx = saturate( round_half_away(x / s_x) + zp_x )        # per-tensor
    2. qw stored at calibration time, per output channel
    3. acc = sum_k (qx - zp_x) * (qw - zp_w)   # int64, bounds checked
    4. bq  = round_half_away(b / (s_x * s_w_o))# bias in accumulator scale
    5. qo  = saturate( round_half_away(
              (acc + bq) * s_x * s_w_o / s_out_o ) + zp_out_o )
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .errors import (
    OutOfCalibrationRangeError,
    QuantizationParameterError,
)
from .tensor_types import (
    PerChannelQParams,
    QParams,
    dtype_bounds,
    saturate,
)

# Relative slack added when checking that an input lies inside the calibrated
# representable range. Float construction of requests may land a few ulps
# outside; anything beyond that is a genuine out-of-range rejection.
RANGE_CHECK_SLACK = 1e-9


def round_half_away(values: np.ndarray) -> np.ndarray:
    """Round to nearest, ties away from zero (deterministic, no banker's)."""
    values = np.asarray(values, dtype=np.float64)
    abs_r = np.floor(np.abs(values) + 0.5)
    return np.where(values >= 0.0, abs_r, -abs_r)


# ---------------------------------------------------------------------------
# Per-tensor calibration / quantize / dequantize (activations).
# ---------------------------------------------------------------------------


def calibrate_activation(values: np.ndarray, dtype: Any = np.dtype("int8")) -> QParams:
    """Calibrate one per-tensor ``(scale, zero_point)`` pair.

    The observed range always includes zero, so zero is representable. A
    degenerate (single-valued) batch is extended by +/-1.0 real unit so the
    scale stays strictly positive and the value keeps headroom.
    """
    values = np.asarray(values, dtype=np.float64)
    if values.ndim != 2 or values.size == 0:
        raise QuantizationParameterError(
            "activation calibration expects a non-empty 2-D array",
            shape=tuple(values.shape),
        )
    if not np.all(np.isfinite(values)):
        raise QuantizationParameterError("calibration values must be finite")
    return _calibrate_range(values.min(), values.max(), dtype)


def _calibrate_range(rmin: float, rmax: float, dtype: Any) -> QParams:
    dtype = np.dtype(dtype)
    qmin, qmax = dtype_bounds(dtype)
    rmin = float(min(rmin, 0.0))
    rmax = float(max(rmax, 0.0))
    if rmin == rmax:
        rmin -= 1.0
        rmax += 1.0
    scale = (rmax - rmin) / (qmax - qmin)
    zero_point = int(round_half_away(np.array(qmin - rmin / scale)).item())
    zero_point = max(qmin, min(qmax, zero_point))
    return QParams(scale=float(scale), zero_point=zero_point, dtype=dtype)


def quantize_tensor(
    values: np.ndarray,
    params: QParams,
    *,
    check_range: bool = False,
) -> np.ndarray:
    """Encode a real 2-D tensor: ``q = saturate(round(x / s) + zp)``."""
    values = np.asarray(values, dtype=np.float64)
    if values.ndim != 2:
        from .errors import TensorValidationError

        raise TensorValidationError(
            "values must be 2-D", shape=tuple(values.shape)
        )
    if check_range:
        _assert_within_range(values, params)
    q = round_half_away(values / params.scale) + params.zero_point
    return saturate(q, params.dtype)


def _assert_within_range(values: np.ndarray, params: QParams) -> None:
    qmin, qmax = dtype_bounds(params.dtype)
    rmin = params.scale * (qmin - params.zero_point)
    rmax = params.scale * (qmax - params.zero_point)
    slack = RANGE_CHECK_SLACK * max(1.0, abs(rmin), abs(rmax))
    lo = float(values.min(initial=np.inf)) if values.size else 0.0
    hi = float(values.max(initial=-np.inf)) if values.size else 0.0
    if lo < rmin - slack or hi > rmax + slack:
        raise OutOfCalibrationRangeError(
            "input values fall outside the frozen calibrated range; "
            "the model must be recalibrated, scales are not re-estimated "
            "per request",
            observed_min=lo,
            observed_max=hi,
            calibrated_min=rmin,
            calibrated_max=rmax,
        )


def dequantize_tensor(values_int: np.ndarray, params: QParams) -> np.ndarray:
    """Inverse affine map: ``x = s * (q - zp)``."""
    return params.scale * (values_int.astype(np.float64) - params.zero_point)


# ---------------------------------------------------------------------------
# Per-output-channel calibration / quantize / dequantize (weights).
# ---------------------------------------------------------------------------


def calibrate_weights(
    weights: np.ndarray,
    dtype: Any = np.dtype("int8"),
    *,
    symmetric: bool = False,
) -> PerChannelQParams:
    """Calibrate ``(scale_o, zp_o)`` per output channel (rows of ``weights``)."""
    weights = np.asarray(weights, dtype=np.float64)
    if weights.ndim != 2 or weights.size == 0:
        raise QuantizationParameterError(
            "weight calibration expects a non-empty 2-D (out, in) array",
            shape=tuple(weights.shape),
        )
    if not np.all(np.isfinite(weights)):
        raise QuantizationParameterError("weight calibration values must be finite")
    dtype = np.dtype(dtype)
    qmin, qmax = dtype_bounds(dtype)
    out_features = weights.shape[0]
    scales = np.empty(out_features, dtype=np.float64)
    zps = np.empty(out_features, dtype=np.int64)
    for o in range(out_features):
        row = weights[o]
        if symmetric:
            # zp fixed at 0; scale derived from the larger magnitude.
            amax = float(max(abs(row.min()), abs(row.max()), 1e-12))
            scales[o] = amax / max(-qmin, qmax)
            zps[o] = 0
        else:
            p = _calibrate_range(float(row.min()), float(row.max()), dtype)
            scales[o] = p.scale
            zps[o] = p.zero_point
    return PerChannelQParams(scales=scales, zero_points=zps, dtype=dtype)


def quantize_weights_per_channel(
    weights: np.ndarray, params: PerChannelQParams
) -> np.ndarray:
    """Encode weights with independent scale/zero point per output channel."""
    weights = np.asarray(weights, dtype=np.float64)
    if weights.ndim != 2 or weights.shape[0] != params.num_channels:
        from .errors import TensorValidationError

        raise TensorValidationError(
            "weights shape must match per-channel params",
            weights_shape=tuple(weights.shape),
            channels=params.num_channels,
        )
    q = round_half_away(weights / params.scales[:, None]) + params.zero_points[:, None]
    return saturate(q, params.dtype)


def dequantize_per_channel(
    weights_int: np.ndarray, params: PerChannelQParams
) -> np.ndarray:
    """``W_hat[o, k] = s_o * (q[o, k] - zp_o)`` (channels along axis 0)."""
    return params.scales[:, None] * (
        weights_int.astype(np.float64) - params.zero_points[:, None]
    )


def dequantize_activations_per_channel(
    values_int: np.ndarray, params: PerChannelQParams
) -> np.ndarray:
    """``y_hat[b, o] = s_o * (q[b, o] - zp_o)`` (channels along axis 1)."""
    values_int = np.asarray(values_int)
    if values_int.ndim != 2 or values_int.shape[1] != params.num_channels:
        from .errors import TensorValidationError

        raise TensorValidationError(
            "activations must be 2-D with channels on axis 1",
            shape=tuple(values_int.shape),
            channels=params.num_channels,
        )
    return params.scales[None, :] * (
        values_int.astype(np.float64) - params.zero_points[None, :]
    )


# ---------------------------------------------------------------------------
# Output calibration and bias scale.
# ---------------------------------------------------------------------------


def calibrate_output_channel(
    output_min: np.ndarray,
    output_max: np.ndarray,
    dtype: Any = np.dtype("int8"),
) -> PerChannelQParams:
    """Frozen per-channel parameters for encoded outputs, from calibration."""
    out_dtype = np.dtype(dtype)
    qmin, qmax = dtype_bounds(out_dtype)
    mins = np.asarray(output_min, dtype=np.float64).reshape(-1)
    maxs = np.asarray(output_max, dtype=np.float64).reshape(-1)
    if mins.shape != maxs.shape or np.any(mins > maxs):
        raise QuantizationParameterError(
            "output calibration requires min <= max per channel",
            mins=mins.tolist(),
            maxs=maxs.tolist(),
        )
    scales = np.empty(mins.shape[0], dtype=np.float64)
    zps = np.empty(mins.shape[0], dtype=np.int64)
    for o, (lo, hi) in enumerate(zip(mins, maxs)):
        p = _calibrate_range(float(lo), float(hi), out_dtype)
        scales[o] = p.scale
        zps[o] = p.zero_point
    return PerChannelQParams(scales=scales, zero_points=zps, dtype=out_dtype)


def bias_scale(input_scale: float, weight_scales: np.ndarray) -> np.ndarray:
    """Accumulator-domain bias scale per output channel: ``s_x * s_w_o``."""
    weight_scales = np.asarray(weight_scales, dtype=np.float64)
    return float(input_scale) * weight_scales


def quantize_bias(
    bias: np.ndarray, input_scale: float, weight_scales: np.ndarray
) -> np.ndarray:
    """Quantize bias into the accumulator scale, int64.

    ``bq_o = round(b_o / (s_x * s_w_o))``. The bias scale must match the
    product of the scales actually multiplied in the MAC - using the input
    scale alone (a common bug) is wrong for per-channel weights.
    """
    bias = np.asarray(bias, dtype=np.float64).reshape(-1)
    sb = bias_scale(input_scale, weight_scales)
    if bias.shape != sb.shape:
        raise QuantizationParameterError(
            "bias length must match output channels",
            bias_length=int(bias.shape[0]),
            channels=int(sb.shape[0]),
        )
    bq = round_half_away(bias / sb)
    int64_info = np.iinfo(np.int64)
    if np.any(bq < int64_info.min) or np.any(bq > int64_info.max):
        raise QuantizationParameterError(
            "quantized bias does not fit int64 accumulator",
            min_bq=float(bq.min()),
            max_bq=float(bq.max()),
        )
    return bq.astype(np.int64)


def _as_per_channel(params: "QParams | PerChannelQParams", channels: int) -> PerChannelQParams:
    """Normalize output params to per-channel form.

    A per-tensor :class:`QParams` is broadcast to all channels (intermediate
    activations feeding the next layer); per-channel params are passed through.
    """
    if isinstance(params, PerChannelQParams):
        if params.num_channels != channels:
            raise QuantizationParameterError(
                "output params channel count mismatch",
                params_channels=params.num_channels,
                expected_channels=channels,
            )
        return params
    return PerChannelQParams(
        scales=np.full(channels, params.scale, dtype=np.float64),
        zero_points=np.full(channels, params.zero_point, dtype=np.int64),
        dtype=params.dtype,
    )


def requantize(
    accumulator: np.ndarray,
    bias_q: np.ndarray,
    input_scale: float,
    weight_params: PerChannelQParams,
    output_params: "QParams | PerChannelQParams",
) -> tuple[np.ndarray, np.ndarray]:
    """Steps 4-5 of the integer pipeline.

    ``output_params`` may be per-channel (final outputs) or per-tensor
    (intermediate activation feeding the next layer). Returns
    ``(q_out, saturated_mask)``. Multiplier
    ``M_o = s_x * s_w_o / s_out_o`` is applied as a float64 one-shot
    multiplier (equivalent to the fixed-point Q-multiplier/shift sequence,
    but free of an extra shift truncation). The bias is added in int64
    *before* widening.
    """
    acc = np.asarray(accumulator, dtype=np.int64)
    if acc.ndim != 2:
        from .errors import TensorValidationError

        raise TensorValidationError(
            "accumulator must be 2-D", shape=tuple(acc.shape)
        )
    channels = acc.shape[1]
    out_pc = _as_per_channel(output_params, channels)
    bias_q = np.asarray(bias_q, dtype=np.int64).reshape(-1)
    if bias_q.shape != (channels,):
        from .errors import TensorValidationError

        raise TensorValidationError(
            "bias length must match accumulator channels",
            bias_length=int(bias_q.shape[0]),
            channels=channels,
        )
    total = acc + bias_q  # int64; safety pre-checked in kernel
    multiplier = float(input_scale) * weight_params.scales / out_pc.scales
    scaled = total.astype(np.float64) * multiplier[None, :]
    q = round_half_away(scaled) + out_pc.zero_points[None, :]
    qmin, qmax = dtype_bounds(out_pc.dtype)
    saturated = (q < qmin) | (q > qmax)
    q_out = saturate(q, out_pc.dtype)
    return q_out, saturated
