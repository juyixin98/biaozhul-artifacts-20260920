"""Quantized tensor types and their invariants.

Quantization convention (asymmetric affine)::

    real_value = scale * (q_value - zero_point)

* Activations are per-tensor quantized: one ``(scale, zero_point)`` for the
  whole tensor.
* Weights are per-output-channel quantized: ``scale`` and ``zero_point`` are
  1-D arrays indexed by output channel.

Integer dtypes are explicit (``int8`` / ``uint8`` / ``int32``). Saturation
and accumulator bounds are derived from the dtype - nothing here relies on
NumPy/Python integer wraparound or undefined C overflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import QuantizationParameterError, TensorValidationError

# ---------------------------------------------------------------------------
# Integer domain helpers - the single source of truth for saturation.
# ---------------------------------------------------------------------------

_INT_DTYPE_INFO: dict[Any, tuple[int, int]] = {
    np.dtype("int8"): (-128, 127),
    np.dtype("uint8"): (0, 255),
    np.dtype("int16"): (-32768, 32767),
    np.dtype("int32"): (-2147483648, 2147483647),
    np.dtype("int64"): (-9223372036854775808, 9223372036854775807),
}

SUPPORTED_Q_DTYPES = ("int8", "uint8")
"""Dtypes in which quantized activations and weights may be stored."""

ACCUMULATOR_DTYPE = np.dtype("int64")
"""Accumulator width.

int32 is the classic choice, but it can only hold int8 dot products up to
K = 2**24. We accumulate in int64 and check the *static* worst-case bound
explicitly (see kernels), so overflow is never left to the host language.
"""


def dtype_bounds(dtype: Any) -> tuple[int, int]:
    """Return ``(qmin, qmax)`` for an explicit integer dtype."""
    key = np.dtype(dtype)
    try:
        return _INT_DTYPE_INFO[key]
    except KeyError as exc:
        raise TensorValidationError(
            f"unsupported integer dtype {key!r}; supported: "
            f"{sorted(str(d) for d in _INT_DTYPE_INFO)}"
        ) from exc


def saturate(values: np.ndarray, dtype: Any) -> np.ndarray:
    """Clip to the dtype range and cast, without host-language wraparound.

    Clipping happens in float/int64 space *before* the narrowing cast, so an
    out-of-range value saturates instead of wrapping (NumPy cast wrapping is
    undefined for signed narrowing and is never relied upon).
    """
    qmin, qmax = dtype_bounds(dtype)
    clipped = np.clip(values, qmin, qmax)
    return clipped.astype(np.dtype(dtype), copy=False)


# ---------------------------------------------------------------------------
# Quantization parameter containers.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QParams:
    """Per-tensor quantization parameters (activations)."""

    scale: float
    zero_point: int
    dtype: np.dtype

    def __post_init__(self) -> None:
        dtype = np.dtype(self.dtype)
        qmin, qmax = dtype_bounds(dtype)
        if dtype not in (np.dtype("int8"), np.dtype("uint8")):
            raise QuantizationParameterError(
                "QParams dtype must be int8 or uint8", dtype=str(dtype)
            )
        if not np.isfinite(self.scale) or self.scale <= 0.0:
            raise QuantizationParameterError(
                "scale must be a finite positive number", scale=float(self.scale)
            )
        if not isinstance(self.zero_point, (int, np.integer)):
            raise QuantizationParameterError(
                "zero_point must be an integer", zero_point=repr(self.zero_point)
            )
        if not qmin <= int(self.zero_point) <= qmax:
            raise QuantizationParameterError(
                "zero_point outside dtype range",
                zero_point=int(self.zero_point),
                qmin=qmin,
                qmax=qmax,
            )
        object.__setattr__(self, "dtype", dtype)

    @property
    def qmin(self) -> int:
        return dtype_bounds(self.dtype)[0]

    @property
    def qmax(self) -> int:
        return dtype_bounds(self.dtype)[1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scale": self.scale,
            "zero_point": self.zero_point,
            "dtype": self.dtype.name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QParams":
        return cls(
            scale=float(data["scale"]),
            zero_point=int(data["zero_point"]),
            dtype=np.dtype(str(data["dtype"])),
        )


@dataclass(frozen=True)
class PerChannelQParams:
    """Per-output-channel quantization parameters (weights and output).

    Shapes are ``(out_channels,)``. Output channel ``o`` uses
    ``scale[o]`` / ``zero_point[o]``.
    """

    scales: np.ndarray
    zero_points: np.ndarray
    dtype: np.dtype

    def __post_init__(self) -> None:
        scales = np.asarray(self.scales, dtype=np.float64)
        zps = np.asarray(self.zero_points)
        dtype = np.dtype(self.dtype)
        if dtype not in (np.dtype("int8"), np.dtype("uint8")):
            raise QuantizationParameterError(
                "PerChannelQParams dtype must be int8 or uint8", dtype=str(dtype)
            )
        if scales.ndim != 1:
            raise QuantizationParameterError(
                "scales must be 1-D (out_channels,)", shape=tuple(scales.shape)
            )
        if zps.shape != scales.shape:
            raise QuantizationParameterError(
                "zero_points shape must match scales",
                scales_shape=tuple(scales.shape),
                zp_shape=tuple(zps.shape),
            )
        if not np.all(np.isfinite(scales)) or np.any(scales <= 0.0):
            bad = int(np.argmax(~np.isfinite(scales) | (scales <= 0.0)))
            raise QuantizationParameterError(
                "all scales must be finite positive numbers",
                bad_index=bad,
                bad_scale=float(scales.flat[bad]),
            )
        qmin, qmax = dtype_bounds(dtype)
        if not np.issubdtype(zps.dtype, np.integer):
            raise QuantizationParameterError(
                "zero_points must be integer", dtype=str(zps.dtype)
            )
        if np.any(zps < qmin) or np.any(zps > qmax):
            bad = int(
                np.argmax((zps < qmin) | (zps > qmax))
            )
            raise QuantizationParameterError(
                "zero_point outside dtype range",
                bad_index=bad,
                bad_zero_point=int(zps.flat[bad]),
                qmin=qmin,
                qmax=qmax,
            )
        object.__setattr__(self, "scales", scales)
        object.__setattr__(self, "zero_points", zps.astype(np.int64))
        object.__setattr__(self, "dtype", dtype)

    @property
    def num_channels(self) -> int:
        return int(self.scales.shape[0])

    def to_dict(self) -> dict[str, Any]:
        return {
            "scales": self.scales.tolist(),
            "zero_points": [int(z) for z in self.zero_points],
            "dtype": self.dtype.name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PerChannelQParams":
        return cls(
            scales=np.asarray(data["scales"], dtype=np.float64),
            zero_points=np.asarray(data["zero_points"], dtype=np.int64),
            dtype=np.dtype(str(data["dtype"])),
        )


# ---------------------------------------------------------------------------
# Quantized tensors.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QTensor:
    """A quantized activation tensor with per-tensor parameters.

    ``values`` holds *real* 2-D activations (batch x in_features) plus the
    frozen parameters used to encode them. The encoded integer view is
    :meth:`quantized`, produced deterministically (round-half-away-from-zero).
    Keeping the real values makes the type a safe request payload; the kernel
    path always works on the encoded integers.
    """

    values: np.ndarray
    params: QParams

    def __post_init__(self) -> None:
        values = np.asarray(self.values, dtype=np.float64)
        if values.ndim != 2:
            raise TensorValidationError(
                "QTensor values must be 2-D (batch, in_features)",
                shape=tuple(values.shape),
            )
        if not np.all(np.isfinite(values)):
            raise TensorValidationError(
                "QTensor values must all be finite", shape=tuple(values.shape)
            )
        object.__setattr__(self, "values", values)

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(self.values.shape)

    def quantized(self) -> np.ndarray:
        """Encoded int view: ``q = saturate(round(x / s) + zp)``."""
        from .quantize import quantize_tensor  # local: avoid import cycle

        return quantize_tensor(self.values, self.params)


@dataclass(frozen=True)
class QWeightMatrix:
    """Per-channel quantized weight matrix of shape ``(out_features, in_features)``."""

    weights_int: np.ndarray
    params: PerChannelQParams

    def __post_init__(self) -> None:
        w = np.asarray(self.weights_int)
        if w.ndim != 2:
            raise TensorValidationError(
                "weights must be 2-D (out_features, in_features)",
                shape=tuple(w.shape),
            )
        if w.dtype != self.params.dtype:
            raise TensorValidationError(
                "weights dtype does not match parameters",
                weights_dtype=str(w.dtype),
                params_dtype=str(self.params.dtype),
            )
        qmin, qmax = dtype_bounds(w.dtype)
        if np.any(w < qmin) or np.any(w > qmax):
            raise TensorValidationError(
                "weights contain values outside dtype range",
                shape=tuple(w.shape),
                qmin=qmin,
                qmax=qmax,
            )
        if self.params.num_channels != w.shape[0]:
            raise TensorValidationError(
                "per-channel params must have one entry per output channel",
                out_features=int(w.shape[0]),
                params_channels=self.params.num_channels,
            )
        object.__setattr__(self, "weights_int", w.copy())

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(self.weights_int.shape)

    def dequantized(self) -> np.ndarray:
        """Real-valued view ``w = s_o * (q - zp_o)``, per output channel."""
        from .quantize import dequantize_per_channel

        return dequantize_per_channel(self.weights_int, self.params)
