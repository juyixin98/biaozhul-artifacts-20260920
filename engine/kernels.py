"""Explicit integer MAC kernel.

The kernel never lets saturation or overflow be decided by host-language
integer behavior:

* Products and sums are computed in int64 workspace, then the result is
  checked against the *declared* accumulator width (int32 or int64).
* Before computing anything, the *static* worst case is checked against that
  width, and a typed :class:`AccumulatorOverflowError` (REJECT) is raised when
  a dot product cannot fit.
* Output saturation is explicit and reported back per element.

Zero-point handling
--------------------
The MAC accumulates de-zero-pointed operands::

    acc[b, o] = sum_k (qx[b, k] - zp_x) * (qw[o, k] - zp_w[o])

The zero-point correction is folded as expanded integer terms so the
mathematical order is unambiguous (no float subtraction anywhere)::

    (qx-zp_x)*(qw-zp_w) = qx*qw - qx*zp_w - zp_x*qw + zp_x*zp_w
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import AccumulatorOverflowError, TensorValidationError
from .quantize import quantize_tensor, requantize
from .tensor_types import (
    PerChannelQParams,
    QParams,
    QTensor,
    QWeightMatrix,
    dtype_bounds,
)

SUPPORTED_ACCUMULATOR_DTYPES = (np.dtype("int32"), np.dtype("int64"))
DEFAULT_ACCUMULATOR_DTYPE = np.dtype("int32")
"""int32 is the classic accumulator for int8 MAC. int64 can be declared for
large contraction dimensions; either way the width is explicit."""


@dataclass(frozen=True)
class IntegerMatMulResult:
    """Raw kernel output (before dequantization), useful for exact tests."""

    accumulator: np.ndarray  # declared accumulator dtype, de-zp sums + bias
    mac_without_bias: np.ndarray  # int64, zero-point-corrected dot products
    bias_q: np.ndarray  # int64, accumulator scale
    q_out: np.ndarray  # saturated integer output
    saturated: np.ndarray  # boolean mask of output saturation
    input_int: np.ndarray  # encoded request, for diagnostics/replay
    accumulator_dtype: np.dtype


def static_accumulator_bounds(
    k_dim: int,
    input_params: QParams,
    weight: QWeightMatrix,
    accumulator_dtype: Any = DEFAULT_ACCUMULATOR_DTYPE,
) -> tuple[int, int, int, int]:
    """Worst-case dot-product range for the declared accumulator width.

    Returns ``(mac_min, mac_max, accumulator_min, accumulator_max)`` where the
    first two are the static worst-case magnitudes::

        |a|    <= max(|qmin-zp_x|, |qmax-zp_x|)
        |w_o|  <= max over stored weights of |qw - zp_w_o|
        |mac|  <= K * |a| * max_o |w_o|
    """
    if k_dim <= 0:
        raise TensorValidationError("K dimension must be positive", k_dim=k_dim)
    acc_dtype = np.dtype(accumulator_dtype)
    if acc_dtype not in SUPPORTED_ACCUMULATOR_DTYPES:
        raise TensorValidationError(
            "accumulator dtype must be int32 or int64", dtype=str(acc_dtype)
        )
    i_qmin, i_qmax = dtype_bounds(input_params.dtype)
    a_max = max(
        abs(i_qmin - input_params.zero_point),
        abs(i_qmax - input_params.zero_point),
    )
    wdev = np.abs(
        weight.weights_int.astype(np.int64) - weight.params.zero_points[:, None]
    )
    w_max = int(wdev.max()) if wdev.size else 0
    worst_mag = k_dim * a_max * w_max
    info = np.iinfo(acc_dtype)
    return -worst_mag, worst_mag, int(info.min), int(info.max)


def _resolve_input(
    x: QTensor | np.ndarray, input_params: QParams | None
) -> tuple[np.ndarray, QParams]:
    if isinstance(x, QTensor):
        return x.values, x.params
    x_values = np.asarray(x, dtype=np.float64)
    if input_params is None:
        raise TensorValidationError(
            "input_params required when x is a raw ndarray"
        )
    return x_values, input_params


def integer_linear(
    x: QTensor | np.ndarray,
    weight: QWeightMatrix,
    output_params: "QParams | PerChannelQParams",
    *,
    input_params: QParams | None = None,
    bias_q: np.ndarray | None = None,
    accumulator_dtype: Any = DEFAULT_ACCUMULATOR_DTYPE,
    enforce_calibration_range: bool = True,
) -> IntegerMatMulResult:
    """Run the full integer linear op.

    ``bias_q`` is the bias already quantized into the accumulator scale
    ``s_x * s_w_o`` (see :func:`engine.quantize.quantize_bias`); it is part of
    the frozen model artifact, so its scale is bound at calibration time.
    """
    x_values, params = _resolve_input(x, input_params)
    acc_dtype = np.dtype(accumulator_dtype)
    if acc_dtype not in SUPPORTED_ACCUMULATOR_DTYPES:
        raise TensorValidationError(
            "accumulator dtype must be int32 or int64", dtype=str(acc_dtype)
        )
    if x_values.ndim != 2:
        raise TensorValidationError(
            "input must be 2-D (batch, in_features)", shape=tuple(x_values.shape)
        )
    batch, k_dim = x_values.shape
    out_features, w_k = weight.shape
    if w_k != k_dim:
        raise TensorValidationError(
            "in_features mismatch between input and weights",
            input_features=int(k_dim),
            weight_features=int(w_k),
        )
    if isinstance(output_params, PerChannelQParams):
        if output_params.num_channels != out_features:
            raise TensorValidationError(
                "output params must have one entry per output channel",
                out_features=out_features,
                output_param_channels=output_params.num_channels,
            )
    elif not isinstance(output_params, QParams):
        raise TensorValidationError(
            "output_params must be QParams or PerChannelQParams",
            got=type(output_params).__name__,
        )

    bias = (
        np.zeros(out_features, dtype=np.int64)
        if bias_q is None
        else np.asarray(bias_q, dtype=np.int64).reshape(-1)
    )
    if bias.shape != (out_features,):
        raise TensorValidationError(
            "bias length must match output channels",
            bias_length=int(bias.shape[0]),
            out_features=out_features,
        )

    mac_min, mac_max, lim_min, lim_max = static_accumulator_bounds(
        k_dim, params, weight, acc_dtype
    )
    b_min = int(bias.min()) if bias.size else 0
    b_max = int(bias.max()) if bias.size else 0
    if mac_min + min(b_min, 0) < lim_min or mac_max + max(b_max, 0) > lim_max:
        raise AccumulatorOverflowError(
            "dot product worst case exceeds the declared accumulator width; "
            "refusing to execute under host-language overflow semantics",
            k_dim=int(k_dim),
            accumulator_dtype=str(acc_dtype),
            worst_case_min=int(mac_min + min(b_min, 0)),
            worst_case_max=int(mac_max + max(b_max, 0)),
            accumulator_min=lim_min,
            accumulator_max=lim_max,
        )

    # Step 1: encode the request against the frozen activation params.
    qx = quantize_tensor(
        x_values, params, check_range=enforce_calibration_range
    ).astype(np.int64)
    qw = weight.weights_int.astype(np.int64)
    zp_x = int(params.zero_point)
    zp_w = weight.params.zero_points.astype(np.int64)

    # Step 3: zero-point-corrected integer dot products, via expanded terms.
    mac = (
        qx @ qw.T
        - (qx.sum(axis=1, keepdims=True) * zp_w[None, :])
        - (zp_x * qw.sum(axis=1, keepdims=True).T)
        + k_dim * zp_x * zp_w[None, :]
    )

    # Defense in depth: the static check guarantees fit, but verify the
    # realized values before the narrowing cast instead of trusting wraparound.
    total = mac + bias[None, :]
    if np.any(total < lim_min) or np.any(total > lim_max):
        raise AccumulatorOverflowError(
            "realized accumulator value exceeded the declared width",
            accumulator_dtype=str(acc_dtype),
            observed_min=int(total.min()),
            observed_max=int(total.max()),
        )
    accumulator = total.astype(acc_dtype)

    # Steps 4-5: rescale by M_o = s_x * s_w_o / s_out_o, round half away,
    # add output zero point, explicitly saturate.
    q_out, saturated = requantize(
        mac,
        bias,
        params.scale,
        weight.params,
        output_params,
    )
    return IntegerMatMulResult(
        accumulator=accumulator,
        mac_without_bias=mac,
        bias_q=bias,
        q_out=q_out,
        saturated=saturated,
        input_int=qx.astype(params.dtype),
        accumulator_dtype=acc_dtype,
    )
