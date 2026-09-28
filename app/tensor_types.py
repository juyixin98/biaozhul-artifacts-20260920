"""Tensor types and quantization specifications.

This module is the *single source of truth* for every numerical convention used
in the backend:

* storage dtype, integer bounds and zero-point semantics;
* the affine mapping between real values and integer codes;
* explicit saturation semantics (host ints never silently wrap/overflow);
* per-tensor (activations) vs per-output-channel (weights) parameter layout.

Quantization model
------------------
Real value ``r`` is represented by an integer code ``q``::

    r = scale * (q - zero_point)

* Activation scales are **per-tensor** (one scale, one zero point).
* Weight scales are **per output channel** (one scale/zero point per row of the
  weight matrix).

Activation zero points are free to be non-zero; weight zero points are also
allowed to be non-zero so that the implementation exercises the full
zero-point correction term instead of assuming ``zw = 0``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Storage dtype for every quantized tensor. Signed 8 bit keeps the manual
#: bit-width reasoning in :mod:`app.kernel` honest.
QDTYPE = np.dtype(np.int8)
QDTYPE_NAME = "int8"

#: Signed range of the storage dtype (values are Python ints on purpose: they
#: are used in exact integer arithmetic, never in float computations).
QMIN: int = int(np.iinfo(QDTYPE).min)  # -128
QMAX: int = int(np.iinfo(QDTYPE).max)  #  127


def qbounds(dtype_name: str = QDTYPE_NAME) -> tuple[int, int]:
    """Return the inclusive ``(min, max)`` code range for a storage dtype."""
    if dtype_name != QDTYPE_NAME:
        raise ValueError(f"unsupported quantized dtype: {dtype_name!r}")
    return QMIN, QMAX


# ---------------------------------------------------------------------------
# Saturation — never delegated to host-language integer wraparound
# ---------------------------------------------------------------------------
def saturate(codes: np.ndarray, qmin: int = QMIN, qmax: int = QMAX) -> np.ndarray:
    """Clamp an integer (or floating) array into ``[qmin, qmax]`` and cast to
    the storage dtype.

    Clamping is performed *before* the narrowing cast, so overflow is an
    explicit decision here rather than an artefact of NumPy's cast rules or C
    integer wraparound.
    """
    clipped = np.clip(codes, qmin, qmax)
    return np.rint(clipped).astype(QDTYPE)


def count_saturated(codes: np.ndarray, qmin: int = QMIN, qmax: int = QMAX) -> int:
    """Count elements pinned at either bound (diagnostic/observability)."""
    return int(np.count_nonzero((codes <= qmin) | (codes >= qmax)))


# ---------------------------------------------------------------------------
# Quantization parameters
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class QuantSpec:
    """Affine quantization parameters for one tensor.

    Attributes:
        scale: positive scale(s). Scalar (shape ``()``) for per-tensor
            activations, shape ``(out_channels,)`` for per-channel weights.
        zero_point: integer zero point(s), same broadcast shape as ``scale``.
            Stored as ``int64`` so exact integer math never overflows while
            corrections are accumulated.
        axis: channel axis for per-channel specs; ``None`` for per-tensor.
        dtype_name: storage dtype name (only ``int8`` is supported).
    """

    scale: np.ndarray
    zero_point: np.ndarray
    axis: int | None = None
    dtype_name: str = QDTYPE_NAME

    def __post_init__(self) -> None:
        scale = np.asarray(self.scale, dtype=np.float64)
        zp = np.asarray(self.zero_point)
        qmin, qmax = qbounds(self.dtype_name)
        if scale.ndim == 0:
            if not np.isfinite(float(scale)) or float(scale) <= 0.0:
                raise ValueError("scale must be a finite positive number")
        else:
            if scale.ndim != 1:
                raise ValueError("per-channel scale must be 1-D")
            if not np.all(np.isfinite(scale)) or np.any(scale <= 0.0):
                raise ValueError("every per-channel scale must be finite and positive")
        if zp.shape != scale.shape:
            raise ValueError(
                f"zero_point shape {zp.shape} != scale shape {scale.shape}"
            )
        zp64 = zp.astype(np.int64)
        if np.any(zp64 < qmin) or np.any(zp64 > qmax):
            raise ValueError(f"zero_point out of storage range [{qmin}, {qmax}]")
        # frozen dataclass + object __setattr__ keeps validation-time casts.
        object.__setattr__(self, "scale", scale)
        object.__setattr__(self, "zero_point", zp64)

    @property
    def per_channel(self) -> bool:
        return self.scale.ndim == 1

    @property
    def num_channels(self) -> int:
        return int(self.scale.shape[0]) if self.per_channel else 1

    def real_dtype(self) -> np.dtype:
        return np.dtype(np.float32)


def make_per_tensor_spec(scale: float, zero_point: int) -> QuantSpec:
    """Build an activation-style per-tensor :class:`QuantSpec`."""
    return QuantSpec(scale=np.asarray(scale, dtype=np.float64),
                     zero_point=np.asarray(int(zero_point), dtype=np.int64),
                     axis=None)


def make_per_channel_spec(scales, zero_points) -> QuantSpec:
    """Build a weight-style per-output-channel :class:`QuantSpec`."""
    scales = np.asarray(scales, dtype=np.float64)
    zero_points = np.asarray(zero_points, dtype=np.int64)
    if scales.ndim != 1:
        raise ValueError("per-channel scales must be 1-D")
    return QuantSpec(scale=scales, zero_point=zero_points, axis=0)


# ---------------------------------------------------------------------------
# Quantized tensor values
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class QuantTensor:
    """Integer tensor plus the parameters mapping it back to real values."""

    codes: np.ndarray
    spec: QuantSpec

    def __post_init__(self) -> None:
        codes = np.asarray(self.codes)
        if codes.dtype != QDTYPE:
            raise TypeError(f"codes must be {QDTYPE_NAME}, got {codes.dtype}")
        if self.spec.per_channel:
            axis = self.spec.axis if self.spec.axis is not None else 0
            if codes.ndim == 0 or codes.shape[axis] != self.spec.num_channels:
                raise ValueError(
                    "per-channel dimension mismatch: "
                    f"codes.shape={codes.shape}, axis={axis}, "
                    f"channels={self.spec.num_channels}"
                )
        object.__setattr__(self, "codes", codes)

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(self.codes.shape)

    def dequantize(self) -> np.ndarray:
        """Inverse affine map: ``r = scale * (q - zp)`` (float32 result)."""
        if self.spec.per_channel:
            axis = self.spec.axis if self.spec.axis is not None else 0
            shape = [1] * self.codes.ndim
            shape[axis] = self.spec.num_channels
            scale = self.spec.scale.reshape(shape).astype(np.float32)
            zp = self.spec.zero_point.reshape(shape).astype(np.float32)
            return scale * (self.codes.astype(np.float32) - zp)
        scale = self.spec.scale.astype(np.float32)
        zp = self.spec.zero_point.astype(np.float32)
        return scale * (self.codes.astype(np.float32) - zp)


def quantize_per_tensor(values: np.ndarray, scale: float, zero_point: int) -> QuantTensor:
    """Quantize a real array with a known per-tensor scale/zero point."""
    values = np.asarray(values, dtype=np.float32)
    spec = make_per_tensor_spec(scale, zero_point)
    codes = saturate(np.rint(values / np.float32(scale) + np.float32(zero_point)))
    return QuantTensor(codes=codes, spec=spec)


def quantize_per_channel(values: np.ndarray, scales, zero_points, axis: int = 0) -> QuantTensor:
    """Quantize a real array with known per-channel scale/zero points."""
    values = np.asarray(values, dtype=np.float32)
    spec = make_per_channel_spec(scales, zero_points)
    shape = [1] * values.ndim
    shape[axis] = spec.num_channels
    if values.shape[axis] != spec.num_channels:
        raise ValueError("channel count mismatch between values and scales")
    scale = spec.scale.reshape(shape).astype(np.float32)
    zp = spec.zero_point.reshape(shape).astype(np.float32)
    codes = saturate(np.rint(values / scale + zp))
    return QuantTensor(codes=codes, spec=QuantSpec(spec.scale, spec.zero_point, axis=axis))
