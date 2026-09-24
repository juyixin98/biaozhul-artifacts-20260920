"""M-estimator (robust) kernels used during IRLS/Gauss-Newton.

For an edge with information matrix ``Omega`` and raw residual ``e`` the
(negative log-likelihood style) edge cost is ``rho(s)`` with the
*whitened* squared error

    s = e^T Omega e.

For every kernel we return

    * ``value``      -- rho(s), the contribution to the total cost
    * ``sqrt_weight`` -- sqrt(rho'(s)); scaling the whitened residual by this
                         factor yields the IR
                         LS/Gauss-Newton weight so that

                           J_r^T w J_r  with  w = rho'(s) * Omega

                         is exactly what the Gauss-Newton step minimizes.

The second derivative rho''(s) is exposed for completeness / future
Newton-method use but the optimizer employs the classic IRLS form, which is
standard practice for pose graph optimization (cf. g2o's robust kernels).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

KERNELS = ("none", "huber", "cauchy")


@dataclass(frozen=True)
class KernelResult:
    value: float
    """rho(s): robust edge cost."""
    sqrt_weight: float
    """sqrt(rho'(s)): IRLS residual multiplier."""
    weight: float
    """rho'(s): direct information-matrix multiplier."""


def _none(s: float, delta: float) -> KernelResult:
    return KernelResult(value=s, sqrt_weight=1.0, weight=1.0)


def _huber(s: float, delta: float) -> KernelResult:
    """Huber kernel: quadratic below delta, linear above (g2o convention)."""
    d2 = delta * delta
    if s <= d2:
        return KernelResult(value=s, sqrt_weight=1.0, weight=1.0)
    sqrt_s = np.sqrt(s)
    value = 2.0 * delta * sqrt_s - d2
    weight = delta / sqrt_s
    return KernelResult(value=value, sqrt_weight=np.sqrt(weight), weight=weight)


def _cauchy(s: float, delta: float) -> KernelResult:
    """Cauchy kernel: rho(s) = delta^2 log(1 + s / delta^2)."""
    d2 = delta * delta
    ratio = s / d2
    weight = 1.0 / (1.0 + ratio)
    value = d2 * np.log1p(ratio)
    return KernelResult(value=value, sqrt_weight=np.sqrt(weight), weight=weight)


_IMPL: dict[str, Callable[[float, float], KernelResult]] = {
    "none": _none,
    "huber": _huber,
    "cauchy": _cauchy,
}


def kernel_scales(squared_error: float, kind: str, delta: float) -> KernelResult:
    """Evaluate a robust kernel at the whitened squared error ``s``."""
    kind = kind.lower()
    if kind not in _IMPL:
        raise ValueError(f"unknown kernel {kind!r}; expected one of {KERNELS}")
    if delta <= 0.0:
        raise ValueError("kernel delta must be positive")
    if squared_error < 0.0:
        # guard against tiny negative values from floating-point roundoff
        if squared_error > -1e-12:
            squared_error = 0.0
        else:
            raise ValueError("squared error must be non-negative")
    return _IMPL[kind](float(squared_error), float(delta))
