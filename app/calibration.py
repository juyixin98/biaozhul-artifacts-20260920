"""Offline calibration.

Calibration is an **offline, one-time** procedure: observers collect min/max
statistics over representative synthetic data and produce a frozen
:class:`CalibrationBundle`. The bundle is bound to a ``(model_id, version)``
pair and loaded with the model — the request path never mutates, re-estimates
or even instantiates an observer. That invariant is enforced structurally
(:class:`~app.graph.ModelRegistry` only accepts a frozen bundle) and is also
asserted in the test suite.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from .tensor_types import (
    QMAX,
    QMIN,
    QuantSpec,
    make_per_channel_spec,
    make_per_tensor_spec,
)


class MinMaxObserver:
    """Running min/max observer for a per-tensor activation.

    Accumulates extrema across calibration batches. ``estimate`` is callable
    exactly once per offline calibration run; the returned parameters are
    subsequently treated as immutable.
    """

    def __init__(self) -> None:
        self._min = np.inf
        self._max = -np.inf
        self._batches = 0

    def observe(self, values: np.ndarray) -> None:
        values = np.asarray(values, dtype=np.float32)
        if values.size == 0:
            return
        if not np.all(np.isfinite(values)):
            raise ValueError("calibration values must all be finite")
        self._min = min(self._min, float(np.min(values)))
        self._max = max(self._max, float(np.max(values)))
        self._batches += 1

    @property
    def batches(self) -> int:
        return self._batches

    def estimate(self) -> tuple[float, int]:
        """Return affine ``(scale, zero_point)`` covering the observed range.

        Standard affine min/max mapping onto the full signed int8 range::

            scale = (rmax - rmin) / (qmax - qmin)
            zp    = clamp(round(qmin - rmin / scale), qmin, qmax)

        A dead range (min == max) gets an arbitrary finite positive scale so
        that zero is representable.
        """
        if self._batches == 0:
            raise ValueError("cannot estimate without any observed batch")
        rmin, rmax = float(self._min), float(self._max)
        return affine_params(rmin, rmax)


def affine_params(rmin: float, rmax: float) -> tuple[float, int]:
    """Affine min/max -> (scale, zero_point), independently testable."""
    if not (np.isfinite(rmin) and np.isfinite(rmax)):
        raise ValueError("calibration extrema must be finite")
    if rmin > rmax:
        raise ValueError(f"rmin {rmin} > rmax {rmax}")
    if rmax - rmin < 1e-12:
        # Constant channel/tensor: center the code range on the value.
        scale = max(abs(rmax), 1.0) / 64.0
        zp = int(np.clip(round(QMIN - rmin / scale), QMIN, QMAX))
        return float(scale), zp
    scale = (rmax - rmin) / (QMAX - QMIN)
    zp = int(np.clip(round(QMIN - rmin / scale), QMIN, QMAX))
    return float(scale), int(zp)


def calibrate_weight_per_channel(weight: np.ndarray, axis: int = 1):
    """Return per-output-channel ``(scales, zero_points)`` for weight rows.

    Args:
        weight: ``(N, K)`` float weights; statistics run over axis ``axis``
            (default: reduction axis 1 → one pair per output channel).
    """
    weight = np.asarray(weight, dtype=np.float32)
    if weight.ndim != 2:
        raise ValueError("weight must be 2-D (N, K)")
    if axis not in (0, 1):
        raise ValueError("only axis 0 or 1 supported")
    reduce_axis = axis
    mins = np.min(weight, axis=reduce_axis).astype(np.float64)
    maxs = np.max(weight, axis=reduce_axis).astype(np.float64)
    scales = np.empty(weight.shape[1 - reduce_axis], dtype=np.float64)
    zps = np.empty(weight.shape[1 - reduce_axis], dtype=np.int64)
    for j, (lo, hi) in enumerate(zip(mins, maxs)):
        scales[j], zps[j] = affine_params(float(lo), float(hi))
    return scales, zps


@dataclass(frozen=True)
class TensorParams:
    """Frozen quantization params for one named tensor."""

    scales: tuple[float, ...]
    zero_points: tuple[int, ...]
    per_channel: bool
    axis: int | None = None

    def spec(self):
        if self.per_channel:
            spec = make_per_channel_spec(self.scales, self.zero_points)
            axis = 0 if self.axis is None else self.axis
            return QuantSpec(spec.scale, spec.zero_point, axis=axis)
        return make_per_tensor_spec(self.scales[0], self.zero_points[0])


@dataclass(frozen=True)
class CalibrationBundle:
    """Immutable calibration artifact bound to a model version.

    Contains per-layer input activation specs (per-tensor), weight specs
    (per output channel) and output specs. Identity is a hash over every
    parameter, so swapping any scale changes the fingerprint.
    """

    model_id: str
    model_version: str
    created_at: str
    layers: dict[str, dict[str, TensorParams]] = field(default_factory=dict)

    def fingerprint(self) -> str:
        payload = {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "layers": {
                layer: {
                    role: {
                        "scales": list(p.scales),
                        "zero_points": list(p.zero_points),
                        "per_channel": p.per_channel,
                        "axis": p.axis,
                    }
                    for role, p in roles.items()
                }
                for layer, roles in sorted(self.layers.items())
            },
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def layer(self, name: str) -> dict[str, TensorParams]:
        try:
            return self.layers[name]
        except KeyError as exc:
            raise KeyError(f"no calibration for layer {name!r}") from exc


class _PerChannelObserver:
    """Per-feature min/max over 2-D calibration batches ``(M, N)``."""

    def __init__(self, n: int) -> None:
        self._min = np.full(n, np.inf, dtype=np.float64)
        self._max = np.full(n, -np.inf, dtype=np.float64)
        self._batches = 0

    def observe(self, values: np.ndarray) -> None:
        values = np.asarray(values, dtype=np.float32)
        if values.ndim != 2 or values.shape[1] != self._min.shape[0]:
            raise ValueError("per-channel observer expects (M, N) with fixed N")
        if not np.all(np.isfinite(values)):
            raise ValueError("calibration values must all be finite")
        self._min = np.minimum(self._min, np.min(values, axis=0))
        self._max = np.maximum(self._max, np.max(values, axis=0))
        self._batches += 1

    def estimate(self):
        if self._batches == 0:
            raise ValueError("cannot estimate without any observed batch")
        scales = np.empty_like(self._min)
        zps = np.empty_like(self._min, dtype=np.int64)
        for j, (lo, hi) in enumerate(zip(self._min, self._max)):
            scales[j], zps[j] = affine_params(float(lo), float(hi))
        return scales, zps


class CalibrationBuilder:
    """Collects layer observers and builds a frozen bundle (offline only)."""

    def __init__(self, model_id: str, model_version: str) -> None:
        self._model_id = model_id
        self._model_version = model_version
        self._inputs: dict[str, MinMaxObserver] = {}
        self._outputs: dict[str, _PerChannelObserver] = {}

    def observe_input(self, layer_name: str, values: np.ndarray) -> None:
        self._inputs.setdefault(layer_name, MinMaxObserver()).observe(values)

    def observe_output(self, layer_name: str, values: np.ndarray) -> None:
        values = np.asarray(values, dtype=np.float32)
        if values.ndim != 2:
            raise ValueError("layer output calibration expects 2-D (M, N)")
        obs = self._outputs.get(layer_name)
        if obs is None:
            obs = _PerChannelObserver(values.shape[1])
            self._outputs[layer_name] = obs
        obs.observe(values)

    def build(self, weights: dict[str, np.ndarray]) -> CalibrationBundle:
        """Freeze activation/weight/output observers into an immutable bundle.

        ``weights`` maps each layer name to its ``(N, K)`` float weight
        matrix. Every layer must have received input *and* output
        observations; the request path never performs this step.
        """
        layers: dict[str, dict[str, TensorParams]] = {}
        for name, obs in self._inputs.items():
            if name not in weights:
                raise ValueError(f"missing float weights for layer {name!r}")
            if name not in self._outputs:
                raise ValueError(f"missing output observations for layer {name!r}")
            s, zp = obs.estimate()
            entry = {"input": TensorParams((s,), (zp,), per_channel=False)}
            ws, wzp = calibrate_weight_per_channel(weights[name])
            entry["weight"] = TensorParams(
                tuple(float(x) for x in ws), tuple(int(x) for x in wzp),
                per_channel=True, axis=0,
            )
            os, ozp = self._outputs[name].estimate()
            entry["output"] = TensorParams(
                tuple(float(x) for x in os), tuple(int(x) for x in ozp),
                per_channel=True, axis=1,
            )
            layers[name] = entry
        return CalibrationBundle(
            model_id=self._model_id,
            model_version=self._model_version,
            created_at=datetime.now(timezone.utc).isoformat(),
            layers=layers,
        )
