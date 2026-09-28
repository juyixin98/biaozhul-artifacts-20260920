"""Fixed-point kernels.

Numerical contract, in execution order
---------------------------------------
Given per-tensor activation quant params ``(sa, za)`` and per-output-channel
weight params ``(sw[j], zw[j])``::

    a[i,k] = sa * (qa[i,k] - za)
    w[j,k] = sw[j] * (qw[j,k] - zw[j])

All integer work happens in a **checked int64 workspace** (so NumPy/C integer
wraparound can never decide a result); the mathematically specified
accumulator is int32, and crossing its envelope is an explicit
:class:`AccumulatorOverflow`.

1. ``raw[i,j] = sum_k qa[i,k] * qw[j,k]``
2. ``sumx[i]  = sum_k qa[i,k]``,  ``sumw[j] = sum_k qw[j,k]``
3. Zero-point correction, per output channel::

       acc[i,j] = raw[i,j]
                  - zw[j] * sumx[i]
                  - za    * sumw[j]
                  + K * za * zw[j]
                  + bias_q[j]

4. Requantization, in this exact order::

       yq = saturate_int8( round_half_away( acc * mul[j] ) + zy[j] )

   ``mul[j] = sa * sw[j] / sy[j]`` is float64. Rounding is deterministic
   round-half-away-from-zero (never the host C library rounding mode), and
   saturation happens *before* the narrowing cast to int8.

Accumulator width
-----------------
int8*int8 products fit in int16; an int32 accumulator is provably safe when
``K * 128**2 < 2**31`` (``K <= 131071``). The kernel never assumes that bound:
every stage is checked against the int32 envelope and overflow is raised, not
saturated or wrapped.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .tensor_types import QMIN, QuantSpec, QuantTensor, saturate

#: Mathematically specified accumulator width.
ACC_DTYPE = np.dtype(np.int32)
ACC_MIN = int(np.iinfo(np.int32).min)
ACC_MAX = int(np.iinfo(np.int32).max)

#: Internal workspace width (only used so overflow can be *observed*).
WORK_MIN = int(np.iinfo(np.int64).min)
WORK_MAX = int(np.iinfo(np.int64).max)

#: Worst |int8 * int8| product: both operands may be -128, giving 128*128.
PRODUCT_BOUND = (-QMIN) * (-QMIN)  # 16384
SAFE_K = (2**31 - 1) // PRODUCT_BOUND  # 131071


class AccumulatorOverflow(OverflowError):
    """Raised when the int32 accumulator envelope would be crossed.

    Deliberate checked failure: silently saturating the accumulator would lose
    information and silently wrapping would let the host runtime pick the
    result.
    """


@dataclass(frozen=True)
class MatMulResult:
    """Exact integer intermediates plus the final quantized output.

    Intermediates are retained so tests can assert *specific* integer values
    and diagnostics can explain an accept/reject verdict.
    """

    raw: np.ndarray          # int32 sum_k qa*qw
    sum_x: np.ndarray        # int32 sum_k qa per activation row
    sum_w: np.ndarray        # int32 sum_k qw per output channel
    corrected: np.ndarray    # int32 after zero-point correction + bias
    output: QuantTensor      # int8 requantized result


def round_half_away(values: np.ndarray) -> np.ndarray:
    """Deterministic round-half-away-from-zero as int64.

    NumPy's ``rint`` is banker's rounding; this backend specifies symmetric
    halves away from zero so error sign never depends on even/odd parity.
    """
    values = np.asarray(values, dtype=np.float64)
    if not np.all(np.isfinite(values)):
        raise AccumulatorOverflow("non-finite value reached rounding stage")
    signs = np.sign(values)
    return (np.floor(np.abs(values) + 0.5) * signs).astype(np.int64)


def _check_envelope(values: np.ndarray, *, low: int, high: int, stage: str) -> None:
    vmax = int(np.max(values))
    vmin = int(np.min(values))
    if vmax > high or vmin < low:
        raise AccumulatorOverflow(
            f"envelope exceeded at stage {stage!r}: "
            f"min={vmin}, max={vmax}, allowed=[{low}, {high}]"
        )


def quantized_matmul(
    act: QuantTensor,
    weight: QuantTensor,
    *,
    out_spec: QuantSpec,
    bias_q: np.ndarray | None = None,
) -> MatMulResult:
    """Exact-integer asymmetric per-channel quantized matmul.

    Args:
        act: ``(M, K)`` int8 activations with a **per-tensor** spec.
        weight: ``(N, K)`` int8 weights, per-output-channel spec (axis 0).
        out_spec: ``(N,)`` per-output-channel output spec ``(sy, zy)``.
        bias_q: optional ``(N,)`` int32 bias in accumulator scale
            ``sa * sw[j]`` (see :func:`quantize_bias`).

    Raises:
        ValueError: static shape/param contract violation.
        AccumulatorOverflow: the int32 accumulator envelope was crossed.
    """
    if act.spec.per_channel:
        raise ValueError("activation quantization must be per-tensor")
    if not weight.spec.per_channel or weight.spec.axis not in (None, 0):
        raise ValueError("weight quantization must be per output channel on axis 0")
    if not out_spec.per_channel or out_spec.num_channels != weight.spec.num_channels:
        raise ValueError("output spec needs one (scale, zp) per output channel")
    if out_spec.axis not in (None, 1):
        raise ValueError("output per-channel axis must be 1 for an (M, N) result")

    qa, qw = act.codes, weight.codes
    if qa.ndim != 2 or qw.ndim != 2:
        raise ValueError(f"expected 2-D operands, got {qa.shape} and {qw.shape}")
    m, k = qa.shape
    n, k2 = qw.shape
    if k != k2:
        raise ValueError(f"reduction dimension mismatch: act K={k}, weight K={k2}")

    # Reject absurd K before int64 workspace arithmetic could itself wrap:
    # |raw| <= K*127^2 and the full correction adds at most another 3*K*127^2.
    stage_bound = 4 * k * PRODUCT_BOUND
    if stage_bound > WORK_MAX:
        raise AccumulatorOverflow(
            f"reduction axis K={k} too large for checked int64 workspace"
        )

    # --- int64 workspace: host integer overflow cannot decide anything. -----
    qa64 = qa.astype(np.int64)
    qw64 = qw.astype(np.int64)
    raw = np.matmul(qa64, qw64.T)
    _check_envelope(raw, low=-k * PRODUCT_BOUND, high=k * PRODUCT_BOUND, stage="raw")

    sum_x = qa64.sum(axis=1)                       # (M,)
    sum_w = qw64.sum(axis=1)                       # (N,)
    za = int(act.spec.zero_point)
    zw = weight.spec.zero_point.astype(np.int64)   # (N,)

    # correction = zw*sumx + za*sumw - K*za*zw ;  acc = raw - correction
    correction = (
        zw[np.newaxis, :] * sum_x[:, np.newaxis]
        + np.int64(za) * sum_w[np.newaxis, :]
        - np.int64(k) * np.int64(za) * zw[np.newaxis, :]
    )
    corrected = raw - correction
    _check_envelope(corrected, low=-stage_bound, high=stage_bound, stage="zp_correction")

    if bias_q is not None:
        bias_q = np.asarray(bias_q)
        if bias_q.shape != (n,):
            raise ValueError(f"bias_q must have shape ({n},), got {bias_q.shape}")
        corrected = corrected + bias_q.astype(np.int64)

    # The mathematical contract: the accumulator is int32.
    _check_envelope(corrected, low=ACC_MIN, high=ACC_MAX, stage="accumulator")

    # Requantize: float64 multiplier, deterministic rounding, zp, saturate.
    sa = act.spec.scale.astype(np.float64)
    sw = weight.spec.scale.astype(np.float64)
    sy = out_spec.scale.astype(np.float64)
    zy = out_spec.zero_point.astype(np.int64)
    multiplier = sa * sw / sy                     # (N,)
    scaled = corrected.astype(np.float64) * multiplier[np.newaxis, :]
    rounded = round_half_away(scaled)
    yq = saturate(rounded + zy[np.newaxis, :])

    return MatMulResult(
        raw=raw.astype(np.int32),
        sum_x=sum_x.astype(np.int32),
        sum_w=sum_w.astype(np.int32),
        corrected=corrected.astype(np.int32),
        output=QuantTensor(codes=yq, spec=out_spec),
    )


def quantize_bias(
    bias_real: np.ndarray,
    act_spec: QuantSpec,
    weight_spec: QuantSpec,
) -> np.ndarray:
    """Quantize a float bias into the accumulator scale ``sa * sw[j]``.

    The bias is added *before* requantization, so its scale is the product of
    the activation scale and the per-channel weight scale — using the output
    scale here would silently rescale every bias term.
    """
    bias_real = np.asarray(bias_real, dtype=np.float64)
    if bias_real.shape != (weight_spec.num_channels,):
        raise ValueError(
            f"bias shape {bias_real.shape} does not match "
            f"{weight_spec.num_channels} output channels"
        )
    if act_spec.per_channel:
        raise ValueError("activation spec must be per-tensor")
    bias_scale = act_spec.scale.astype(np.float64) * weight_spec.scale.astype(np.float64)
    codes = round_half_away(bias_real / bias_scale)
    _check_envelope(codes, low=ACC_MIN, high=ACC_MAX, stage="bias")
    return codes.astype(np.int32)
